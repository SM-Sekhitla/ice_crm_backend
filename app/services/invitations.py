"""One-time invitation tokens and a durable, encrypted email outbox."""

import asyncio
import base64
import hashlib
import logging
import smtplib
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import quote

from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy import select, update

from app.core.config import get_settings
from app.core.security import hash_password, now, opaque_token, token_hash
from app.db.models import RefreshToken, Sender, User, UserInvitation
from app.db.session import SessionLocal
from app.services.common import audit
from app.services.email import DeliveryRejected, brevo_send
from app.services.email_templates import invitation_email

logger = logging.getLogger("crm.invitations")
INVALID_LINK = (
    "This invitation is invalid, expired, or already used. Ask your administrator for a new invitation."
)


def token_cipher():
    # Domain-separated key. Outbox tokens are encrypted; verification stores only SHA-256 digests.
    key = hashlib.sha256(("ice-crm/invitations/v1:" + get_settings().secret_key).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def invitation_url(token):
    return get_settings().frontend_base_url + "/activate#token=" + quote(token, safe="")


async def invitation_sender(session):
    settings = get_settings()
    if settings.email_provider == "brevo":
        email, name = settings.brevo_from_email, settings.brevo_from_name
    else:
        email, name = settings.smtp_from_email, settings.smtp_from_name
    if not email:
        sender = await session.scalar(
            select(Sender).where(Sender.status == "Verified").order_by(Sender.created_at).limit(1)
        )
        if sender:
            email, name = sender.email, sender.sender_name
    if not email:
        raise HTTPException(409, "Configure SMTP_FROM_EMAIL or a verified sender before inviting users")
    return SimpleNamespace(email=email, sender_name=name, reply_to=email)


async def require_invitation_delivery(session):
    settings = get_settings()
    if not settings.email_delivery_enabled:
        raise HTTPException(
            409, "Enable EMAIL_DELIVERY_ENABLED and configure SMTP or Brevo before inviting users"
        )
    await invitation_sender(session)
    if settings.frontend_base_url in ("https://app.example.com", "http://app.example.com"):
        raise HTTPException(409, "Set FRONTEND_BASE_URL to your actual frontend URL before inviting users")


async def queue_invitation(session, user, actor):
    # Callers lock the user before rotating tokens; old links become invalid immediately.
    await session.execute(
        update(UserInvitation)
        .where(UserInvitation.user_id == user.id, UserInvitation.used_at.is_(None))
        .values(revoked=True, encrypted_token=None)
    )
    token = opaque_token()
    record = UserInvitation(
        user_id=user.id,
        created_by=actor.id,
        digest=token_hash(token),
        encrypted_token=token_cipher().encrypt(token.encode()).decode(),
        expires_at=now() + timedelta(hours=get_settings().invitation_expiry_hours),
    )
    session.add(record)
    await session.flush()
    audit(session, actor, "Queued account invitation")
    return record


async def find_invitation(session, token):
    record = await session.scalar(select(UserInvitation).where(UserInvitation.digest == token_hash(token)))
    if not record or record.revoked or record.used_at or record.expires_at <= now():
        raise HTTPException(400, INVALID_LINK)
    return record


async def verify_invitation(session, token):
    record = await find_invitation(session, token)
    user = await session.get(User, record.user_id)
    if not user or not user.active or user.deleted_at is not None or user.password_set:
        raise HTTPException(400, INVALID_LINK)
    return record


async def accept_invitation(session, token, password):
    candidate = await find_invitation(session, token)
    # Same lock order as resend/dispatch: user first, then invitation.
    user = await session.scalar(select(User).where(User.id == candidate.user_id).with_for_update())
    record = await session.scalar(
        select(UserInvitation)
        .where(UserInvitation.id == candidate.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        not user
        or not user.active
        or user.deleted_at is not None
        or user.password_set
        or record.revoked
        or record.used_at
        or record.expires_at <= now()
    ):
        raise HTTPException(400, INVALID_LINK)
    user.password_hash = await asyncio.to_thread(hash_password, password)
    user.password_set = True
    user.onboarding_completed = False
    user.token_version += 1
    record.used_at = now()
    record.encrypted_token = None
    await session.execute(
        update(UserInvitation)
        .where(UserInvitation.user_id == user.id, UserInvitation.id != record.id)
        .values(revoked=True, encrypted_token=None)
    )
    await session.execute(update(RefreshToken).where(RefreshToken.user_id == user.id).values(revoked=True))
    audit(session, user, "Accepted account invitation and set password")
    await session.flush()
    return user


async def process_invitation():
    if not get_settings().email_delivery_enabled:
        return False
    async with SessionLocal() as session, session.begin():
        # Lock a user first to serialize with acceptance/resend; other workers skip it.
        user = await session.scalar(
            select(User)
            .where(
                User.id.in_(select(UserInvitation.user_id).where(UserInvitation.delivery_status == "Queued"))
            )
            .order_by(User.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if not user:
            return False
        record = await session.scalar(
            select(UserInvitation)
            .where(UserInvitation.user_id == user.id, UserInvitation.delivery_status == "Queued")
            .order_by(UserInvitation.created_at)
            .with_for_update()
            .limit(1)
        )
        if not record:
            return False
        if (
            record.revoked
            or record.used_at
            or record.expires_at <= now()
            or not user.active
            or user.deleted_at is not None
            or user.password_set
        ):
            record.delivery_status = "Cancelled"
            record.encrypted_token = None
            record.processed_at = now()
            return True
        try:
            sender = await invitation_sender(session)
        except HTTPException:
            record.delivery_status = "Failed"
            record.delivery_reason = "Invitation sender is not configured"
            record.encrypted_token = None
            return True
        try:
            token = token_cipher().decrypt(record.encrypted_token.encode()).decode()
        except Exception:
            record.delivery_status = "Failed"
            record.delivery_reason = "Invitation token unavailable; resend the invitation"
            record.encrypted_token = None
            return True
        settings = get_settings()
        body = invitation_email(
            user.first_name,
            invitation_url(token),
            settings.frontend_base_url,
            settings.invitation_expiry_hours,
        )
        email, record_id = user.email, record.id
        record.delivery_status = "Dispatching"
        record.processed_at = now()
    # Dispatch marker is committed before contacting the provider. Never blindly retry.
    try:
        if get_settings().email_provider == "brevo":
            await brevo_send(sender, email, "Your ICE CRM invitation", body, record_id)
        else:
            from app.services.worker import smtp_send

            await asyncio.to_thread(smtp_send, sender, email, "Your ICE CRM invitation", body, record_id)
        status, reason = "Accepted", "Provider accepted invitation email"
    except (DeliveryRejected, smtplib.SMTPRecipientsRefused):
        status, reason = "Failed", "Provider rejected invitation email; check configuration and resend"
    except Exception as error:
        logger.warning(
            "Invitation delivery uncertain invitation_id=%s error_type=%s", record_id, type(error).__name__
        )
        status, reason = "Unknown", "Delivery uncertain; check provider logs before resending"
    async with SessionLocal() as session, session.begin():
        record = await session.get(UserInvitation, record_id)
        record.delivery_status = status
        record.delivery_reason = reason
        record.encrypted_token = None
        record.processed_at = now()
    return True


async def recover_invitation_dispatches():
    async with SessionLocal() as session, session.begin():
        await session.execute(
            update(UserInvitation)
            .where(
                UserInvitation.delivery_status == "Dispatching",
                UserInvitation.processed_at < now() - timedelta(minutes=5),
            )
            .values(
                delivery_status="Unknown",
                delivery_reason="Worker interrupted; check provider logs before resending",
                encrypted_token=None,
            )
        )
