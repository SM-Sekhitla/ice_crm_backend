"""Public, rate-limited contact collection through revocable personal QR links."""

import base64
import hashlib
from datetime import timedelta
from urllib.parse import quote

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException
from pydantic import EmailStr, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.api.deps import Session, require
from app.api.schemas import Schema
from app.core.config import get_settings
from app.core.security import now, opaque_token, token_hash
from app.db.models import Contact, ContactIntakeLink, User
from app.services.common import audit

router = APIRouter(tags=["Contact intake"])
INVALID_LINK = "This contact form is unavailable. Please ask your contact for a new QR code."
RECEIVED = {"message": "Thank you. Your details have been received."}


def cipher():
    key = hashlib.sha256(("ice-crm/contact-intake/v1:" + get_settings().secret_key).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def link_response(record):
    if not record or not record.enabled:
        return {"enabled": False, "url": None}
    try:
        token = cipher().decrypt(record.encrypted_token.encode()).decode()
    except InvalidToken:
        raise HTTPException(409, "Replace your QR code to generate a new link") from None
    return {
        "enabled": True,
        "url": get_settings().frontend_base_url + "/connect#token=" + quote(token, safe=""),
    }


class LinkInput(Schema):
    replace: bool = False


class VerifyInput(Schema):
    token: str = Field(pattern=r"^[A-Za-z0-9_-]{64}$")


class IntakeInput(VerifyInput):
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    email: EmailStr = Field(max_length=254)
    phone: str = Field(default="", max_length=50)
    company: str = Field(default="", max_length=200)
    job_title: str = Field(default="", max_length=150)


@router.get("/contact-intake-link")
async def get_link(session: Session, user=Depends(require("contacts.create"))):
    return link_response(await session.get(ContactIntakeLink, user.id))


@router.post("/contact-intake-link")
async def create_link(body: LinkInput, session: Session, user=Depends(require("contacts.create"))):
    # Serialize first creation and replacement, including across API workers.
    await session.scalar(select(User).where(User.id == user.id).with_for_update())
    record = await session.get(ContactIntakeLink, user.id)
    if not record or not record.enabled or body.replace:
        token = opaque_token()
        values = dict(
            digest=token_hash(token),
            encrypted_token=cipher().encrypt(token.encode()).decode(),
            enabled=True,
        )
        if record:
            for key, value in values.items():
                setattr(record, key, value)
        else:
            record = ContactIntakeLink(
                user_id=user.id, window_started_at=now(), submission_count=0, **values
            )
            session.add(record)
        audit(session, user, "Generated personal contact QR link")
        await session.flush()
    return link_response(record)


@router.delete("/contact-intake-link", status_code=204)
async def disable_link(session: Session, user=Depends(require("contacts.create"))):
    await session.scalar(select(User).where(User.id == user.id).with_for_update())
    record = await session.get(ContactIntakeLink, user.id)
    if record:
        record.enabled = False
        audit(session, user, "Disabled personal contact QR link")


async def available_link(session, token, *, lock=False):
    query = select(ContactIntakeLink).where(
        ContactIntakeLink.digest == token_hash(token), ContactIntakeLink.enabled.is_(True)
    )
    if lock:
        query = query.with_for_update()
    record = await session.scalar(query)
    owner = await session.get(User, record.user_id) if record else None
    if not record or not owner or not owner.active or not owner.password_set or owner.deleted_at is not None:
        raise HTTPException(404, INVALID_LINK)
    return record, owner


@router.post("/public/contact-intake/verify")
async def verify_link(body: VerifyInput, session: Session):
    await available_link(session, body.token)
    return {"available": True}


@router.post("/public/contact-intake/submit")
async def submit_contact(body: IntakeInput, session: Session):
    link, owner = await available_link(session, body.token, lock=True)
    timestamp = now()
    if timestamp - link.window_started_at >= timedelta(hours=1):
        link.window_started_at = timestamp
        link.submission_count = 0
    if link.submission_count >= 300:
        raise HTTPException(
            429, "This form is temporarily busy. Please try again later.",
            headers={"Retry-After": "3600"},
        )
    link.submission_count += 1
    # Never reveal whether an address exists or overwrite an existing contact.
    # Only explicitly submitted fields are accepted; no automatic email/marketing consent.
    result = await session.execute(
        insert(Contact)
        .values(
            **body.model_dump(exclude={"token", "email"}),
            email=str(body.email).lower(),
            captured_by=owner.id,
            source="QR contact form",
            consent=False,
        )
        .on_conflict_do_nothing(index_elements=[Contact.email])
        .returning(Contact.id)
    )
    contact_id = result.scalar_one_or_none()
    if contact_id:
        audit(session, owner, "Contact received through personal QR form", contact_id)
    return RECEIVED
