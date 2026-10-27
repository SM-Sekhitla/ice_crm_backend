from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select

from app.api.deps import CurrentUser, Session, assigned, association_scope, product_access, require
from app.api.schemas import CampaignInput, Launch, SegmentInput, SenderInput, SignatureInput
from app.core.config import get_settings
from app.core.permissions import ADMIN
from app.core.security import now
from app.db.models import (
    Assignment,
    Association,
    AudienceSegment,
    Campaign,
    Contact,
    Product,
    Recipient,
    Sender,
    Signature,
    User,
)
from app.services.common import audit, serialize
from app.services.email_html import personalize, sanitize_html

router = APIRouter(tags=["Campaigns"])


async def campaign_record(session, user, campaign_id, lock=False):
    query = select(Campaign).where(Campaign.id == campaign_id)
    if user.role != ADMIN:
        query = query.where(
            Campaign.created_by == user.id,
            Campaign.product_id.in_(select(Assignment.product_id).where(Assignment.user_id == user.id)),
        )
    if lock:
        query = query.with_for_update()
    record = await session.scalar(query)
    if not record:
        raise HTTPException(404, "Campaign not found")
    return record


async def signature_record(session, user, signature_id):
    record = await session.get(Signature, signature_id)
    if not record or (user.role != ADMIN and record.owner_id != user.id):
        raise HTTPException(404, "Signature not found")
    return record


async def eligible_contact(session, owner, product_id, contact_id):
    if (
        not owner
        or not owner.active
        or (owner.role != ADMIN and not await assigned(session, owner.id, product_id))
    ):
        return None
    return await session.scalar(
        select(Contact)
        .join(Association, Contact.id == Association.contact_id)
        .where(
            Contact.id == contact_id,
            Association.product_id == product_id,
            association_scope(owner),
            Contact.consent.is_(True),
            Contact.do_not_email.is_(False),
        )
    )


async def validate_references(session, owner, body):
    await product_access(session, owner, body.product_id)
    product = await session.get(Product, body.product_id)
    if not owner.active or not product or product.status != "Active":
        raise HTTPException(422, "Campaign owner and product must be active")
    if body.sender_id:
        sender = await session.get(Sender, body.sender_id)
        if (
            not sender
            or body.product_id not in sender.product_ids
            or (owner.role != ADMIN and owner.id not in sender.user_ids)
        ):
            raise HTTPException(422, "Sender is not available to this campaign owner/product")
    if body.signature_id:
        await signature_record(session, owner, body.signature_id)
    for cid in body.recipient_ids:
        if not await eligible_contact(session, owner, body.product_id, cid):
            raise HTTPException(422, "Audience includes an unavailable or suppressed contact")


@router.get("/signatures")
async def signatures(session: Session, user: CurrentUser):
    query = select(Signature).order_by(Signature.name)
    if user.role != ADMIN:
        query = query.where(Signature.owner_id == user.id)
    return [serialize(r) for r in await session.scalars(query)]


@router.post("/signatures", status_code=201)
async def create_signature(body: SignatureInput, session: Session, user=Depends(require("campaigns.manage"))):
    clean = sanitize_html(body.html)
    if not clean.strip():
        raise HTTPException(422, "Signature has no visible content")
    record = Signature(name=body.name, html=clean, owner_id=user.id, source_file_name=body.source_file_name)
    session.add(record)
    audit(session, user, "Uploaded email signature")
    await session.flush()
    return serialize(record)


@router.delete("/signatures/{signature_id}", status_code=204)
async def delete_signature(signature_id: str, session: Session, user=Depends(require("campaigns.manage"))):
    record = await signature_record(session, user, signature_id)
    if await session.scalar(select(Campaign.id).where(Campaign.signature_id == signature_id).limit(1)):
        raise HTTPException(409, "Signature is used by a campaign")
    await session.delete(record)
    audit(session, user, "Deleted unused email signature")


@router.get("/senders")
async def senders(session: Session, user: CurrentUser):
    query = select(Sender)
    if user.role != ADMIN:
        query = query.where(Sender.user_ids.contains([user.id]))
    return [serialize(r) for r in await session.scalars(query)]


async def sender_values(session, body):
    for pid in body.product_ids:
        if not await session.get(Product, pid):
            raise HTTPException(422, "Unknown sender product")
    for uid in body.user_ids:
        if not await session.get(User, uid):
            raise HTTPException(422, "Unknown sender user")
    values = body.model_dump()
    values["email"] = str(body.email).lower()
    values["reply_to"] = str(body.reply_to).lower()
    return values


@router.post("/senders", status_code=201)
async def create_sender(body: SenderInput, session: Session, user=Depends(require("email_config.manage"))):
    record = Sender(**await sender_values(session, body))
    session.add(record)
    audit(session, user, "Created sender configuration")
    await session.flush()
    return serialize(record)


@router.put("/senders/{sender_id}")
async def update_sender(
    sender_id: str, body: SenderInput, session: Session, user=Depends(require("email_config.manage"))
):
    record = await session.get(Sender, sender_id)
    if not record:
        raise HTTPException(404, "Sender not found")
    active = await session.scalar(
        select(Campaign.id)
        .where(Campaign.sender_id == sender_id, Campaign.status.in_(["Scheduled", "Sending"]))
        .limit(1)
    )
    if active:
        raise HTTPException(409, "Sender is used by an active campaign")
    for key, value in (await sender_values(session, body)).items():
        setattr(record, key, value)
    audit(session, user, "Updated sender configuration")
    return serialize(record)


@router.get("/audience-segments")
async def segments(session: Session, user: CurrentUser):
    records = await session.scalars(select(AudienceSegment).where(AudienceSegment.created_by == user.id))
    return [serialize(r) for r in records]


@router.post("/audience-segments", status_code=201)
async def save_segment(body: SegmentInput, session: Session, user=Depends(require("campaigns.manage"))):
    await product_access(session, user, body.product_id)
    for cid in body.contact_ids:
        if not await eligible_contact(session, user, body.product_id, cid):
            raise HTTPException(422, "Audience contains unavailable contacts")
    record = AudienceSegment(**body.model_dump(), created_by=user.id)
    session.add(record)
    await session.flush()
    return serialize(record)


@router.delete("/audience-segments/{segment_id}", status_code=204)
async def delete_segment(segment_id: str, session: Session, user=Depends(require("campaigns.manage"))):
    record = await session.get(AudienceSegment, segment_id)
    if not record or record.created_by != user.id:
        raise HTTPException(404, "Audience segment not found")
    await session.delete(record)


@router.get("/campaigns")
async def campaigns(
    session: Session,
    user=Depends(require("campaigns.view")),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    status: str | None = None,
):
    query = select(Campaign)
    if user.role != ADMIN:
        query = query.where(
            Campaign.created_by == user.id,
            Campaign.product_id.in_(select(Assignment.product_id).where(Assignment.user_id == user.id)),
        )
    if status:
        query = query.where(Campaign.status == status)
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    records = await session.scalars(query.order_by(Campaign.created_at.desc()).offset(offset).limit(limit))
    return {"items": [serialize(r) for r in records], "total": total}


@router.post("/campaigns", status_code=201)
async def create_campaign(body: CampaignInput, session: Session, user=Depends(require("campaigns.manage"))):
    await validate_references(session, user, body)
    record = Campaign(
        **body.model_dump(exclude={"body_html"}), body_html=sanitize_html(body.body_html), created_by=user.id
    )
    session.add(record)
    audit(session, user, "Created campaign draft", product_id=body.product_id)
    await session.flush()
    return serialize(record)


@router.get("/campaigns/{campaign_id}")
async def get_campaign(campaign_id: str, session: Session, user=Depends(require("campaigns.view"))):
    return serialize(await campaign_record(session, user, campaign_id))


@router.put("/campaigns/{campaign_id}")
async def edit_campaign(
    campaign_id: str, body: CampaignInput, session: Session, user=Depends(require("campaigns.manage"))
):
    record = await campaign_record(session, user, campaign_id, lock=True)
    if record.status != "Draft":
        raise HTTPException(409, "Only drafts can be edited")
    owner = await session.get(User, record.created_by)
    await validate_references(session, owner, body)
    for key, value in body.model_dump().items():
        setattr(record, key, sanitize_html(value) if key == "body_html" else value)
    audit(session, user, "Updated campaign draft", product_id=record.product_id)
    await session.flush()
    return serialize(record)


@router.post("/campaigns/{campaign_id}/duplicate", status_code=201)
async def duplicate(campaign_id: str, session: Session, user=Depends(require("campaigns.manage"))):
    original = await campaign_record(session, user, campaign_id)
    values = {field: getattr(original, field) for field in CampaignInput.model_fields}
    values["name"] = original.name[:140] + " (copy)"
    # Recheck access in the new owner's context, never copy unavailable IDs silently.
    return await create_campaign(CampaignInput(**values), session, user)


@router.delete("/campaigns/{campaign_id}", status_code=204)
async def delete_campaign(campaign_id: str, session: Session, user=Depends(require("campaigns.manage"))):
    record = await campaign_record(session, user, campaign_id, lock=True)
    if record.status != "Draft":
        raise HTTPException(409, "Only unsent drafts can be deleted")
    audit(session, user, "Deleted campaign draft", product_id=record.product_id)
    await session.delete(record)


async def recipient_tokens(session, campaign, owner, contact):
    product = await session.get(Product, campaign.product_id)
    sender = await session.get(Sender, campaign.sender_id) if campaign.sender_id else None
    return {
        "first_name": contact.first_name,
        "last_name": contact.last_name,
        "company_name": contact.company,
        "product_name": product.name,
        "assigned_bdo": f"{owner.first_name} {owner.last_name}",
        "sender_name": sender.sender_name if sender else "",
    }


@router.post("/campaigns/{campaign_id}/preview/{contact_id}")
async def preview(
    campaign_id: str, contact_id: str, session: Session, user=Depends(require("campaigns.view"))
):
    record = await campaign_record(session, user, campaign_id)
    owner = await session.get(User, record.created_by)
    contact = await eligible_contact(session, owner, record.product_id, contact_id)
    if not contact:
        raise HTTPException(404, "Eligible contact not found")
    signature = await signature_record(session, owner, record.signature_id) if record.signature_id else None
    tokens = await recipient_tokens(session, record, owner, contact)
    return {
        "subject": personalize(record.subject, tokens),
        "bodyHtml": sanitize_html(personalize(record.body_html, tokens)),
        "signatureHtml": sanitize_html(personalize(signature.html if signature else "", tokens)),
    }


@router.post("/campaigns/{campaign_id}/launch")
async def launch(campaign_id: str, body: Launch, session: Session, user=Depends(require("campaigns.send"))):
    record = await campaign_record(session, user, campaign_id, lock=True)
    if record.status != "Draft":
        raise HTTPException(409, "Campaign already submitted")
    owner = await session.get(User, record.created_by)
    await validate_references(session, owner, record)
    if not record.subject or not record.body_html or not record.recipient_ids or not record.sender_id:
        raise HTTPException(422, "Subject, body, sender and recipients are required")
    sender = await session.get(Sender, record.sender_id)
    if sender.status != "Verified":
        raise HTTPException(422, "Sender must be verified by an administrator")
    if body.mode == "smtp" and not get_settings().email_delivery_enabled:
        raise HTTPException(409, "Real email delivery is disabled")
    if body.scheduled_at and body.scheduled_at <= now():
        raise HTTPException(422, "Schedule must be in the future")
    record.mode = body.mode
    record.status = "Scheduled" if body.scheduled_at else "Sending"
    record.scheduled_at = body.scheduled_at
    if record.signature_id:
        record.signature_html = (await signature_record(session, owner, record.signature_id)).html
    for cid in record.recipient_ids:
        contact = await eligible_contact(session, owner, record.product_id, cid)
        session.add(
            Recipient(
                campaign_id=record.id,
                contact_id=cid,
                email=contact.email,
                name=f"{contact.first_name} {contact.last_name}",
                tokens=await recipient_tokens(session, record, owner, contact),
            )
        )
    audit(session, user, f"Submitted {body.mode} campaign", product_id=record.product_id)
    await session.flush()
    return serialize(record)


@router.post("/campaigns/{campaign_id}/cancel")
async def cancel(campaign_id: str, session: Session, user=Depends(require("campaigns.manage"))):
    record = await campaign_record(session, user, campaign_id, lock=True)
    if record.status not in ("Scheduled", "Sending"):
        raise HTTPException(409, "Campaign is not queued")
    record.status = "Cancelled"
    for recipient in await session.scalars(
        select(Recipient)
        .where(Recipient.campaign_id == record.id, Recipient.status == "Queued")
        .with_for_update()
    ):
        recipient.status = "Excluded"
        recipient.reason = "Campaign cancelled"
        recipient.processed_at = now()
    audit(session, user, "Cancelled remaining campaign recipients", product_id=record.product_id)
    return serialize(record)


@router.get("/campaigns/{campaign_id}/recipients")
async def recipients(
    campaign_id: str,
    session: Session,
    user=Depends(require("campaigns.view")),
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
):
    await campaign_record(session, user, campaign_id)
    records = await session.scalars(
        select(Recipient)
        .where(Recipient.campaign_id == campaign_id)
        .order_by(Recipient.created_at, Recipient.id)
        .offset(offset)
        .limit(limit)
    )
    return [serialize(r) for r in records]


@router.get("/campaigns/{campaign_id}/analytics")
async def analytics(campaign_id: str, session: Session, user=Depends(require("campaigns.view"))):
    await campaign_record(session, user, campaign_id)
    rows = await session.execute(
        select(Recipient.status, func.count())
        .where(Recipient.campaign_id == campaign_id)
        .group_by(Recipient.status)
    )
    return {
        "counts": dict(rows.all()),
        "opens": None,
        "clicks": None,
        "note": "Accepted means SMTP accepted the message, not confirmed inbox delivery.",
    }
