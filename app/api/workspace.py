"""Authoritative scoped workspace for the existing CRM collection-based UI."""

from fastapi import APIRouter
from sqlalchemy import select

from app.api.auth import user_response
from app.api.deps import CurrentUser, Session, association_scope
from app.core.permissions import ADMIN
from app.db.models import (
    Activity,
    Assignment,
    Association,
    AudienceSegment,
    Campaign,
    Client,
    Contact,
    FollowUp,
    Lead,
    Product,
    Recipient,
    Sender,
    Signature,
    User,
)
from app.services.common import serialize

router = APIRouter(tags=["Workspace"])


@router.get("/workspace")
async def workspace(session: Session, user: CurrentUser):
    # Reads share one repeatable-read snapshot, avoiding partially refreshed relationships.
    await session.commit()
    from sqlalchemy import text

    await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
    association_ids = select(Association.id).where(association_scope(user))
    contact_ids = select(Association.contact_id).where(association_scope(user))
    product_ids = select(Assignment.product_id).where(Assignment.user_id == user.id)
    admin = user.role == ADMIN
    products = select(Product).where(True if admin else Product.id.in_(product_ids))
    campaigns = select(Campaign).where(
        True if admin else (Campaign.created_by == user.id) & Campaign.product_id.in_(product_ids)
    )
    campaign_ids = campaigns.with_only_columns(Campaign.id)
    records = {
        "products": products,
        "contacts": select(Contact).where(True if admin else Contact.id.in_(contact_ids)),
        "associations": select(Association).where(association_scope(user)),
        "leads": select(Lead).where(Lead.association_id.in_(association_ids)),
        "clients": select(Client).where(Client.association_id.in_(association_ids)),
        "followUps": select(FollowUp).where(FollowUp.association_id.in_(association_ids)),
        "campaigns": campaigns,
        "campaignRecipients": select(Recipient).where(Recipient.campaign_id.in_(campaign_ids)),
        "senders": select(Sender).where(True if admin else Sender.user_ids.contains([user.id])),
        "signatures": select(Signature).where(True if admin else Signature.owner_id == user.id),
        "audienceSegments": select(AudienceSegment).where(
            AudienceSegment.created_by == user.id,
            True if admin else AudienceSegment.product_id.in_(product_ids),
        ),
        "activities": select(Activity).where(
            True if admin else (Activity.user_id == user.id) & Activity.product_id.in_(product_ids)
        ),
    }
    data = {"version": 1}
    for name, query in records.items():
        data[name] = [serialize(record) for record in await session.scalars(query)]
    if admin:
        people = list(await session.scalars(select(User).order_by(User.email)))
        data["users"] = [await user_response(session, person) for person in people]
    else:
        # Only the logged-in identity is needed for BDO edits. Do not expose the staff directory.
        data["users"] = [await user_response(session, user)]
    return data
