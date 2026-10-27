from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select

from app.api.deps import (
    CurrentUser,
    Session,
    association_scope,
    get_association,
    product_access,
    require,
    valid_owner,
)
from app.api.schemas import (
    AssociationInput,
    ContactCreate,
    ContactFields,
    ContactNotes,
    FollowUpInput,
    FollowUpStatus,
    Reassign,
    Transition,
)
from app.core.permissions import ADMIN
from app.core.security import now
from app.db.models import Activity, Assignment, Association, Client, Contact, FollowUp, Lead, Product
from app.services.common import audit, serialize

router = APIRouter(tags=["CRM"])


async def contact_record(session, user, contact_id):
    record = await session.scalar(
        select(Contact).where(
            Contact.id == contact_id,
            True
            if user.role == ADMIN
            else Contact.id.in_(select(Association.contact_id).where(association_scope(user))),
        )
    )
    if not record:
        raise HTTPException(404, "Contact not found")
    return record


@router.get("/products")
async def products(session: Session, user: CurrentUser):
    query = select(Product).order_by(Product.name)
    if user.role != ADMIN:
        query = query.where(
            Product.id.in_(select(Assignment.product_id).where(Assignment.user_id == user.id))
        )
    return [serialize(r) for r in await session.scalars(query)]


@router.get("/contacts")
async def contacts(
    session: Session,
    user=Depends(require("contacts.view")),
    q: str = Query("", max_length=200),
    product_id: str | None = Query(None, alias="productId"),
    stage: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
):
    associations = select(Association.contact_id).where(association_scope(user))
    if product_id:
        await product_access(session, user, product_id)
        associations = associations.where(Association.product_id == product_id)
    if stage:
        associations = associations.where(Association.stage == stage)
    query = select(Contact)
    if user.role != ADMIN or product_id or stage:
        query = query.where(Contact.id.in_(associations))
    if q:
        query = query.where(
            or_(
                *(
                    field.ilike(f"%{q}%")
                    for field in (Contact.first_name, Contact.last_name, Contact.email, Contact.company)
                )
            )
        )
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    records = await session.scalars(
        query.order_by(Contact.created_at.desc(), Contact.id).offset(offset).limit(limit)
    )
    return {"items": [serialize(r) for r in records], "total": total, "offset": offset, "limit": limit}


@router.post("/contacts", status_code=201)
async def create_contact(body: ContactCreate, session: Session, user=Depends(require("contacts.create"))):
    if user.role != ADMIN and body.owner_id != user.id:
        raise HTTPException(403, "BDOs can only create their own relationships")
    for pid in set(body.product_ids):
        await product_access(session, user, pid)
        product = await session.get(Product, pid)
        if not product or product.status != "Active":
            raise HTTPException(422, "Product must be active")
        await valid_owner(session, body.owner_id, pid)
    contact = Contact(
        **body.model_dump(exclude={"product_ids", "owner_id", "email"}), email=str(body.email).lower()
    )
    session.add(contact)
    await session.flush()
    for pid in set(body.product_ids):
        session.add(Association(contact_id=contact.id, product_id=pid, owner_id=body.owner_id))
        audit(session, user, "Created contact relationship", contact.id, pid)
    return serialize(contact)


@router.get("/contacts/{contact_id}")
async def get_contact(contact_id: str, session: Session, user=Depends(require("contacts.view"))):
    return serialize(await contact_record(session, user, contact_id))


@router.put("/contacts/{contact_id}")
async def edit_contact(
    contact_id: str, body: ContactFields, session: Session, user=Depends(require("contacts.edit"))
):
    record = await contact_record(session, user, contact_id)
    for key, value in body.model_dump().items():
        setattr(record, key, str(value).lower() if key == "email" else value)
    audit(session, user, "Updated contact", contact_id)
    await session.flush()
    await session.refresh(record)
    return serialize(record)


@router.delete("/contacts/{contact_id}", status_code=204)
async def delete_contact(contact_id: str, session: Session, user=Depends(require("contacts.delete"))):
    record = await contact_record(session, user, contact_id)
    audit(session, user, "Deleted contact and linked lifecycle records")
    await session.delete(record)


@router.get("/associations")
async def associations(
    session: Session, user: CurrentUser, contact_id: str | None = Query(None, alias="contactId")
):
    query = select(Association).where(association_scope(user))
    if contact_id:
        query = query.where(Association.contact_id == contact_id)
    return [serialize(r) for r in await session.scalars(query)]


@router.post("/contacts/{contact_id}/associations", status_code=201)
async def add_association(
    contact_id: str, body: AssociationInput, session: Session, user=Depends(require("contacts.edit"))
):
    await contact_record(session, user, contact_id)
    await product_access(session, user, body.product_id)
    if user.role != ADMIN and body.owner_id != user.id:
        raise HTTPException(403, "Cannot assign another user")
    product = await session.get(Product, body.product_id)
    if not product or product.status != "Active":
        raise HTTPException(422, "Product must be active")
    await valid_owner(session, body.owner_id, body.product_id)
    record = Association(contact_id=contact_id, **body.model_dump())
    session.add(record)
    audit(session, user, "Added product relationship", contact_id, body.product_id)
    await session.flush()
    return serialize(record)


@router.put("/associations/{association_id}/owner")
async def reassign(
    association_id: str, body: Reassign, session: Session, user=Depends(require("users.manage"))
):
    record = await get_association(session, user, association_id, lock=True)
    await valid_owner(session, body.owner_id, record.product_id)
    record.owner_id = body.owner_id
    followups = await session.scalars(
        select(FollowUp).where(FollowUp.association_id == record.id, FollowUp.status == "Open")
    )
    for followup in followups:
        followup.owner_id = body.owner_id
    audit(session, user, "Reassigned relationship", record.contact_id, record.product_id)
    return serialize(record)


@router.delete("/associations/{association_id}", status_code=204)
async def remove_association(association_id: str, session: Session, user=Depends(require("contacts.edit"))):
    record = await get_association(session, user, association_id, lock=True)
    await session.scalar(select(Contact).where(Contact.id == record.contact_id).with_for_update())
    count = await session.scalar(
        select(func.count()).select_from(Association).where(Association.contact_id == record.contact_id)
    )
    if count <= 1:
        raise HTTPException(409, "Cannot remove the last relationship")
    for model in (Lead, Client, FollowUp):
        if await session.scalar(select(model.id).where(model.association_id == record.id).limit(1)):
            raise HTTPException(409, "Relationship has lifecycle history")
    audit(session, user, "Removed product relationship", record.contact_id, record.product_id)
    await session.delete(record)


@router.post("/associations/{association_id}/stage")
async def transition(
    association_id: str, body: Transition, session: Session, user=Depends(require("leads.manage"))
):
    record = await get_association(session, user, association_id, lock=True)
    client = await session.scalar(select(Client).where(Client.association_id == record.id))
    if client and body.stage != "Client":
        raise HTTPException(409, "Converted client history cannot be reverted")
    lead = await session.scalar(select(Lead).where(Lead.association_id == record.id))
    if body.stage not in ("Contact", "Client") and not lead:
        lead = Lead(association_id=record.id)
        session.add(lead)
    if lead:
        if body.expected_value is not None:
            lead.expected_value = body.expected_value
        lead.priority = body.priority
    if body.stage == "Client" and not client:
        session.add(
            Client(
                association_id=record.id,
                deal_value=body.deal_value
                if body.deal_value is not None
                else (lead.expected_value if lead else 0),
                reference=body.reference,
            )
        )
        if lead:
            lead.converted_at = now()
    if body.conversion_date and body.conversion_date > now().date():
        raise HTTPException(422, "Conversion date cannot be in the future")
    if body.stage in ("Lost", "Not Interested") and not body.notes.strip():
        raise HTTPException(422, "Record a reason for this outcome")
    if body.follow_up and body.stage != "Client":
        session.add(
            FollowUp(
                association_id=record.id,
                owner_id=record.owner_id,
                title="Follow up on opportunity",
                due_date=body.follow_up.isoformat(),
                priority=body.priority,
                notes=body.notes,
            )
        )
        await sync_followup(session, record.id)
    if body.stage == "Client" and body.conversion_date:
        await session.flush()
        converted = await session.scalar(select(Client).where(Client.association_id == record.id))
        from datetime import datetime, time, timezone

        converted.client_since = datetime.combine(body.conversion_date, time.min, tzinfo=timezone.utc)
    record.stage = body.stage
    audit(
        session,
        user,
        f"Moved relationship to {body.stage}" + (f": {body.notes}" if body.notes else ""),
        record.contact_id,
        record.product_id,
    )
    await session.flush()
    return serialize(record)


@router.get("/leads")
async def leads(session: Session, user: CurrentUser):
    return [
        serialize(r)
        for r in await session.scalars(
            select(Lead).where(Lead.association_id.in_(select(Association.id).where(association_scope(user))))
        )
    ]


@router.get("/clients")
async def clients(session: Session, user: CurrentUser):
    return [
        serialize(r)
        for r in await session.scalars(
            select(Client).where(
                Client.association_id.in_(select(Association.id).where(association_scope(user)))
            )
        )
    ]


@router.get("/follow-ups")
async def followups(session: Session, user: CurrentUser):
    return [
        serialize(r)
        for r in await session.scalars(
            select(FollowUp)
            .where(FollowUp.association_id.in_(select(Association.id).where(association_scope(user))))
            .order_by(FollowUp.due_date)
        )
    ]


async def sync_followup(session, association_id):
    await session.flush()
    next_due = await session.scalar(
        select(func.min(FollowUp.due_date)).where(
            FollowUp.association_id == association_id, FollowUp.status == "Open"
        )
    )
    lead = await session.scalar(select(Lead).where(Lead.association_id == association_id))
    if lead:
        lead.next_follow_up = next_due or ""


@router.post("/associations/{association_id}/follow-ups", status_code=201)
async def add_followup(
    association_id: str, body: FollowUpInput, session: Session, user=Depends(require("leads.manage"))
):
    association = await get_association(session, user, association_id, lock=True)
    record = FollowUp(
        association_id=association_id,
        owner_id=association.owner_id,
        **body.model_dump(exclude={"due_date"}),
        due_date=body.due_date.isoformat(),
        completed_at=now() if body.status == "Completed" else None,
    )
    session.add(record)
    await sync_followup(session, association_id)
    audit(session, user, "Created follow-up", association.contact_id, association.product_id)
    return serialize(record)


@router.put("/follow-ups/{followup_id}")
async def edit_followup(
    followup_id: str, body: FollowUpInput, session: Session, user=Depends(require("leads.manage"))
):
    record = await session.get(FollowUp, followup_id)
    if not record:
        raise HTTPException(404, "Follow-up not found")
    association = await get_association(session, user, record.association_id, lock=True)
    for key, value in body.model_dump().items():
        setattr(record, key, value.isoformat() if key == "due_date" else value)
    record.completed_at = now() if body.status == "Completed" else None
    await sync_followup(session, record.association_id)
    audit(session, user, "Updated follow-up", association.contact_id, association.product_id)
    return serialize(record)


@router.get("/activities")
async def activities(session: Session, user: CurrentUser, limit: int = Query(100, ge=1, le=500)):
    query = select(Activity)
    if user.role != ADMIN:
        query = query.where(
            Activity.user_id == user.id,
            Activity.product_id.in_(select(Assignment.product_id).where(Assignment.user_id == user.id)),
        )
    return [
        serialize(r) for r in await session.scalars(query.order_by(Activity.created_at.desc()).limit(limit))
    ]


@router.get("/dashboard")
async def dashboard(session: Session, user: CurrentUser):
    scope = select(Association.id).where(association_scope(user))
    counts = {}
    for name, model in [("leads", Lead), ("clients", Client), ("followUps", FollowUp)]:
        query = select(func.count()).select_from(model).where(model.association_id.in_(scope))
        if model == FollowUp:
            query = query.where(FollowUp.status == "Open")
        counts[name] = await session.scalar(query)
    counts["contacts"] = await session.scalar(
        select(func.count(func.distinct(Association.contact_id))).where(association_scope(user))
    )
    counts["pipelineValue"] = await session.scalar(
        select(func.coalesce(func.sum(Lead.expected_value), 0)).where(
            Lead.association_id.in_(scope), Lead.converted_at.is_(None)
        )
    )
    return counts


@router.put("/contacts/{contact_id}/notes", status_code=204)
async def update_notes(
    contact_id: str, body: ContactNotes, session: Session, user=Depends(require("contacts.edit"))
):
    record = await contact_record(session, user, contact_id)
    record.notes = body.notes
    audit(session, user, "Updated contact notes", contact_id)


@router.put("/follow-ups/{followup_id}/status")
async def followup_status(
    followup_id: str, body: FollowUpStatus, session: Session, user=Depends(require("leads.manage"))
):
    record = await session.get(FollowUp, followup_id)
    if not record:
        raise HTTPException(404, "Follow-up not found")
    association = await get_association(session, user, record.association_id, lock=True)
    record.status = body.status
    record.completed_at = now() if body.status == "Completed" else None
    await sync_followup(session, record.association_id)
    audit(session, user, "Updated follow-up status", association.contact_id, association.product_id)
    return serialize(record)
