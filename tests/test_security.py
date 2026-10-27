from datetime import datetime, timedelta, timezone

import pytest
from jose import jwt

from app.core.config import get_settings
from app.core.permissions import has_permission
from app.core.security import decode_token, hash_password, verify_password
from app.services.email_html import personalize, sanitize_html


def test_password_hash_and_permissions():
    encoded = hash_password("example-long-password")
    assert encoded.startswith("$argon2id$")
    assert verify_password("example-long-password", encoded)
    assert not verify_password("wrong", encoded)
    assert not verify_password("wrong", "invalid")
    assert has_permission("Super Admin", "users.manage")
    assert not has_permission("BDO", "users.manage")
    assert not has_permission("unknown", "contacts.view")


def test_expired_or_wrong_type_jwt():
    settings = get_settings()
    token = jwt.encode(
        {
            "sub": "admin",
            "iss": settings.token_issuer,
            "type": "access",
            "ver": 0,
            "iat": datetime.now(timezone.utc),
            "exp": datetime.now(timezone.utc) - timedelta(seconds=1),
        },
        settings.secret_key,
        algorithm="HS256",
    )
    with pytest.raises(ValueError):
        decode_token(token)
    with pytest.raises(ValueError):
        decode_token("untrusted")


def test_personalization_escapes_html_and_unsafe_css():
    value = sanitize_html(personalize("<p>{{first_name}}</p>", {"first_name": "<img src=x onerror=evil()>"}))
    assert "<img" not in value and "&lt;img" in value
    assert "javascript:" not in sanitize_html('<a href="javascript:alert(1)">link</a>')
    assert "url(" not in sanitize_html('<p style="background-image:url(https://example.com)">hello</p>')


def test_embedded_signature_images_become_cid_attachments(monkeypatch):
    from app.core.config import get_settings
    from app.db.models import Sender
    from app.services import worker

    settings = get_settings()
    monkeypatch.setattr(settings, "email_delivery_enabled", True)
    captured = []

    class SMTPStub:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def starttls(self, **kwargs):
            pass

        def send_message(self, message):
            captured.append(message)

    monkeypatch.setattr(worker.smtplib, "SMTP", SMTPStub)
    monkeypatch.setattr(settings, "smtp_username", "")
    image = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jP6kAAAAASUVORK5CYII="
    sender = Sender(sender_name="ICE", email="sender@example.com", reply_to="reply@example.com")
    worker.smtp_send(
        sender,
        "recipient@example.com",
        "Example",
        f'<img src="data:image/png;base64,{image}">',
        "recipient-id",
    )
    parts = list(captured[0].walk())
    assert any(part.get_content_type() == "image/png" for part in parts)
    html_part = next(part for part in parts if part.get_content_type() == "text/html")
    assert "cid:image-0-recipient-id" in html_part.get_content()
    assert "data:image" not in html_part.get_content()
