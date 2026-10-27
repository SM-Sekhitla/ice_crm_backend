"""Durable PostgreSQL queue. Run separately: python -m app.services.worker.

Dispatching is committed BEFORE external delivery. Crashed sends become Unknown
and are never retried automatically, to avoid duplicate messages.
"""

import asyncio
import base64
import logging
import re
import signal
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from sqlalchemy import func, or_, select

from app.api.campaigns import eligible_contact
from app.core.config import get_settings
from app.core.security import now
from app.db.models import Campaign, Product, Recipient, Sender, User
from app.db.session import SessionLocal, engine
from app.services.email import DeliveryRejected, brevo_send
from app.services.email_html import personalize, sanitize_html
from app.services.invitations import process_invitation, recover_invitation_dispatches

logger = logging.getLogger("crm.worker")


def smtp_send(sender, recipient, subject, body, recipient_id):
    settings = get_settings()
    if not settings.email_delivery_enabled:
        raise RuntimeError("Email delivery is disabled")
    message = EmailMessage()
    message["From"] = formataddr((sender.sender_name, sender.email))
    message["To"] = recipient
    message["Reply-To"] = sender.reply_to
    message["Subject"] = subject
    message["Message-ID"] = f"<{recipient_id}@ice-crm.local>"
    embedded = []

    def replace_image(match):
        content_type, data = match[1], match[2]
        cid = f"image-{len(embedded)}-{recipient_id}"
        embedded.append((cid, content_type, base64.b64decode(data)))
        return f'src="cid:{cid}"'

    body = re.sub(r'src="data:image/(png|jpeg|gif|webp);base64,([A-Za-z0-9+/=]+)"', replace_image, body)
    message.set_content("This message contains HTML. Please view it in an HTML-capable email client.")
    message.add_alternative(body, subtype="html")
    for cid, subtype, data in embedded:
        message.get_payload()[1].add_related(data, maintype="image", subtype=subtype, cid=f"<{cid}>")
    client_type = smtplib.SMTP_SSL if settings.smtp_ssl else smtplib.SMTP
    kwargs = {"timeout": 30}
    if settings.smtp_ssl:
        kwargs["context"] = ssl.create_default_context()
    with client_type(settings.smtp_host, settings.smtp_port, **kwargs) as client:
        if settings.smtp_starttls:
            client.starttls(context=ssl.create_default_context())
        if settings.smtp_username:
            client.login(settings.smtp_username, settings.smtp_password)
        client.send_message(message)


async def process_one():
    # Lock the campaign first, matching launch/cancel and preventing deadlocks.
    async with SessionLocal() as session, session.begin():
        campaign = await session.scalar(
            select(Campaign)
            .where(
                Campaign.status.in_(["Scheduled", "Sending"]),
                or_(Campaign.scheduled_at.is_(None), Campaign.scheduled_at <= now()),
            )
            .order_by(Campaign.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if not campaign:
            return False
        campaign.status = "Sending"
        recipient = await session.scalar(
            select(Recipient)
            .where(Recipient.campaign_id == campaign.id, Recipient.status == "Queued")
            .order_by(Recipient.created_at, Recipient.id)
            .with_for_update()
            .limit(1)
        )
        if not recipient:
            counts = dict(
                (
                    await session.execute(
                        select(Recipient.status, func.count())
                        .where(Recipient.campaign_id == campaign.id)
                        .group_by(Recipient.status)
                    )
                ).all()
            )
            if counts.get("Dispatching"):
                return False
            successes = counts.get("Accepted", 0) + counts.get("Simulated", 0)
            failures = counts.get("Failed", 0) + counts.get("Unknown", 0)
            campaign.status = ("Partially Failed" if successes else "Failed") if failures else "Sent"
            campaign.sent_at = now()
            return True
        owner = await session.get(User, campaign.created_by)
        contact = await eligible_contact(session, owner, campaign.product_id, recipient.contact_id)
        sender = await session.get(Sender, campaign.sender_id)
        product = await session.get(Product, campaign.product_id)
        reason = ""
        if not contact or contact.email != recipient.email:
            reason = "Consent, suppression, ownership, or email changed"
        elif not product or product.status != "Active":
            reason = "Product is inactive"
        elif (
            not sender
            or sender.status != "Verified"
            or campaign.product_id not in sender.product_ids
            or (owner.role != "Super Admin" and owner.id not in sender.user_ids)
        ):
            reason = "Sender is unavailable"
        if reason:
            recipient.status = "Excluded"
            recipient.reason = reason
            recipient.processed_at = now()
            return True
        if campaign.mode == "simulation":
            recipient.status = "Simulated"
            recipient.reason = "Simulation only; no email was sent"
            recipient.processed_at = now()
            return True
        recipient.status = "Dispatching"
        recipient.processed_at = now()
        recipient_id = recipient.id
        email = recipient.email
        # Personalisation is escaped and sanitised after substitution.
        subject = re.sub(
            r"\{\{\s*(\w+)\s*\}\}", lambda m: str(recipient.tokens.get(m[1], "")), campaign.subject
        )
        subject = subject.replace("\r", "").replace("\n", "")
        body = sanitize_html(personalize(campaign.body_html + campaign.signature_html, recipient.tokens))
        # Sender attributes remain usable after the transaction (expire_on_commit=False).
    provider = get_settings().email_provider
    try:
        if not get_settings().email_delivery_enabled:
            raise DeliveryRejected("Email delivery is disabled")
        if provider == "brevo":
            message_id = await brevo_send(sender, email, subject, body, recipient_id)
            reason = f"Brevo accepted message; messageId={message_id}"
        else:
            await asyncio.to_thread(smtp_send, sender, email, subject, body, recipient_id)
            reason = "SMTP server accepted message"
        status = "Accepted"
    except DeliveryRejected as exc:
        status, reason = "Failed", str(exc)
    except smtplib.SMTPRecipientsRefused:
        status, reason = "Failed", "SMTP rejected recipient"
    except Exception as exc:
        logger.warning(
            "Uncertain send provider=%s recipient_id=%s error_type=%s",
            provider,
            recipient_id,
            type(exc).__name__,
        )
        status, reason = (
            "Unknown",
            f"{provider.upper()} outcome uncertain; inspect provider logs before manual action",
        )
    async with SessionLocal() as session, session.begin():
        record = await session.get(Recipient, recipient_id)
        record.status = status
        record.reason = reason
        record.processed_at = now()
    return True


async def recover_stale():
    from datetime import timedelta

    async with SessionLocal() as session, session.begin():
        records = await session.scalars(
            select(Recipient)
            .where(Recipient.status == "Dispatching", Recipient.processed_at < now() - timedelta(minutes=5))
            .with_for_update(skip_locked=True)
        )
        for record in records:
            record.status = "Unknown"
            record.reason = (
                "Worker interrupted during email delivery; inspect provider logs before manual action"
            )


async def main():
    logging.basicConfig(level=logging.INFO)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGBREAK"):
        signals.append(signal.SIGBREAK)
    for sig in signals:
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows event loops use ordinary process signal handlers.
            signal.signal(sig, lambda signum, frame: loop.call_soon_threadsafe(stop.set))
    try:
        while not stop.is_set():
            try:
                await recover_stale()
                await recover_invitation_dispatches()
                invited = await process_invitation()
                worked = await process_one() or invited
            except Exception:
                logger.exception("Queue processing failed")
                worked = False
            if not worked:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=2)
                except TimeoutError:
                    pass
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
