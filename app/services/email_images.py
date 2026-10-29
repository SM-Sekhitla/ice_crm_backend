"""Prepare locally uploaded email images without a third-party content library."""

import base64
import hashlib
import hmac
import re
from urllib.parse import urlsplit

from sqlalchemy.dialects.postgresql import insert

from app.db.models import EmailImage
from app.services.email_html import image_source

# Sanitized HTML uses quoted src attributes; also support single quotes/case/spacing.
EMBEDDED_IMAGE = re.compile(
    r"(<img\b[^>]*?\bsrc\s*=\s*)([\"'])(data:image/(png|jpeg|gif|webp);base64,[A-Za-z0-9+/=\s]+)\2",
    re.IGNORECASE,
)


def image_base_url(settings):
    base = (getattr(settings, "email_image_base_url", "") or settings.frontend_base_url).rstrip("/")
    parsed = urlsplit(base)
    if (
        parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
        or parsed.query or parsed.fragment
        or parsed.hostname.lower() in {"localhost", "127.0.0.1", "::1"}
    ):
        raise ValueError(
            "Uploaded images need a public HTTPS CRM URL. Set FRONTEND_BASE_URL "
            "(with /api forwarded to the backend) or EMAIL_IMAGE_BASE_URL to the public backend URL."
        )
    return base


def decode_image(source):
    # Normalize whitespace from imported HTML before strict validation.
    prefix, encoded = source.split(",", 1)
    source = prefix.lower() + "," + re.sub(r"\s+", "", encoded)
    if not image_source(source):
        raise ValueError("Invalid signature image. Upload a PNG, JPEG, GIF or WebP under 250 KB.")
    subtype = prefix.split("/")[1].split(";")[0].lower()
    return subtype, base64.b64decode(source.split(",", 1)[1], validate=True)


def inline_images(body, recipient_id):
    images = {}

    def replace(match):
        subtype, data = decode_image(match[3])
        key = hashlib.sha256(data).hexdigest()
        if key not in images:
            cid = f"image-{key[:24]}-{recipient_id}@crm"
            images[key] = (cid, subtype, data)
        return f'{match[1]}{match[2]}cid:{images[key][0]}{match[2]}'

    return EMBEDDED_IMAGE.sub(replace, body), list(images.values())


async def publish_images(session, body, settings):
    matches = list(EMBEDDED_IMAGE.finditer(body))
    if not matches:
        return body
    base = image_base_url(settings)
    replacements = {}
    for match in matches:
        source = match[3]
        if source in replacements:
            continue
        subtype, data = decode_image(source)
        # Keyed hashes make URLs opaque while deduplicating concurrent sends.
        key = hmac.new(settings.secret_key.encode(), subtype.encode() + b":" + data, hashlib.sha256).hexdigest()
        await session.execute(
            insert(EmailImage).values(id=key, content_type=f"image/{subtype}", data=data)
            .on_conflict_do_nothing(index_elements=[EmailImage.id])
        )
        replacements[source] = f"{base}/api/v1/email-images/{key}"

    return EMBEDDED_IMAGE.sub(
        lambda match: f"{match[1]}{match[2]}{replacements[match[3]]}{match[2]}", body
    )
