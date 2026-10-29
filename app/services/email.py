"""Brevo transport adapted from PRISM, with explicit failures and no secret logging."""

import re

import httpx

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.email_images import EMBEDDED_IMAGE, image_base_url, publish_images

BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"


class DeliveryRejected(Exception):
    """Known non-acceptance. The message is never automatically retried."""


def validate_provider_message(sender, body):
    settings = get_settings()
    if settings.email_provider != "brevo":
        return
    if EMBEDDED_IMAGE.search(body):
        try:
            image_base_url(settings)
        except ValueError as exc:
            raise DeliveryRejected(str(exc)) from exc


async def brevo_send(sender, recipient, subject, body, recipient_id):
    settings = get_settings()
    if not settings.email_delivery_enabled:
        raise DeliveryRejected("Email delivery is disabled")
    if not settings.brevo_api_key.strip() or not sender.email:
        raise DeliveryRejected("Brevo sender configuration is incomplete")
    validate_provider_message(sender, body)
    # Commit before sending: image URLs must work as soon as the email arrives.
    if EMBEDDED_IMAGE.search(body):
        async with SessionLocal() as session, session.begin():
            body = await publish_images(session, body, settings)
    payload = {
        "sender": {"name": sender.sender_name or settings.brevo_from_name, "email": sender.email},
        "to": [{"email": recipient}],
        "replyTo": {"email": sender.reply_to},
        "subject": subject,
        "htmlContent": body,
    }
    # No transport retries or redirects: a timeout may follow provider acceptance.
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        response = await client.post(
            BREVO_API_URL,
            headers={"api-key": settings.brevo_api_key, "accept": "application/json"},
            json=payload,
        )
    if 400 <= response.status_code < 500 and response.status_code != 408:
        raise DeliveryRejected(f"Brevo rejected the request (HTTP {response.status_code})")
    if response.status_code != 201:
        raise RuntimeError("Brevo acceptance could not be confirmed")
    message_id = response.json().get("messageId")
    if not isinstance(message_id, str) or not message_id.strip():
        raise RuntimeError("Brevo response did not confirm a message ID")
    return re.sub(r"[\x00-\x1f]", "", message_id)[:500]
