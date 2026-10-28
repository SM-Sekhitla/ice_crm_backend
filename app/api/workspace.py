"""Shared CRM records with separately scoped administration and campaign data."""

from fastapi import APIRouter
from sqlalchemy import or_, select

from app.api.auth import user_response
from app.api.deps import CurrentUser, Session
from app.core.config import get_settings
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
    UserInvitation,
)
from app.services.common import serialize

router = APIRouter(tags=["Workspace"])


@router.get("/workspace")
async def workspace(session: Session, user: CurrentUser):
    # Reads share one repeatable-read snapshot, avoiding partially refreshed relationships.
    await session.commit()
    from sqlalchemy import text

    await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
    product_ids = select(Assignment.product_id).where(Assignment.user_id == user.id)
    admin = user.role == ADMIN
    products = select(Product)
    campaigns = select(Campaign).where(
        True if admin else (Campaign.created_by == user.id) & Campaign.product_id.in_(product_ids)
    )
    campaign_ids = campaigns.with_only_columns(Campaign.id)
    records = {
        "products": products,
        "contacts": select(Contact),
        "associations": select(Association),
        "leads": select(Lead),
        "clients": select(Client),
        "followUps": select(FollowUp),
        "campaigns": campaigns,
        "campaignRecipients": select(Recipient).where(Recipient.campaign_id.in_(campaign_ids)),
        "senders": select(Sender).where(True if admin else Sender.user_ids.contains([user.id])),
        "signatures": select(Signature).where(True if admin else Signature.owner_id == user.id),
        "audienceSegments": select(AudienceSegment).where(
            AudienceSegment.created_by == user.id,
            True if admin else AudienceSegment.product_id.in_(product_ids),
        ),
        "activities": select(Activity).where(
            True if admin else or_(Activity.contact_id.is_not(None), Activity.user_id == user.id)
        ),
    }
    settings = get_settings()
    data = {
        "version": 1,
        "emailProvider": settings.email_provider,
        "emailDeliveryEnabled": settings.email_delivery_enabled,
        "brevoFromEmail": settings.brevo_from_email,
        "brevoFromName": settings.brevo_from_name,
    }
    for name, query in records.items():
        data[name] = [serialize(record) for record in await session.scalars(query)]
    for sender in data["senders"]:
        sender["provider"] = settings.email_provider.upper()
    if admin:
        people = list(await session.scalars(select(User).order_by(User.email)))
        data["users"] = [await user_response(session, person) for person in people]
        invitations = list(
            await session.execute(
                select(
                    UserInvitation.user_id,
                    UserInvitation.delivery_status,
                    UserInvitation.delivery_reason,
                    UserInvitation.expires_at,
                )
                .where(UserInvitation.revoked.is_(False))
                .order_by(UserInvitation.created_at.desc())
            )
        )
        latest = {}
        for invitation in invitations:
            latest.setdefault(invitation.user_id, invitation)
        for person in data["users"]:
            invitation = latest.get(person["id"])
            if invitation and not person["passwordSet"]:
                person["invitationStatus"] = invitation.delivery_status
                person["invitationReason"] = invitation.delivery_reason
                person["invitationExpiresAt"] = invitation.expires_at
    else:
        # Owner labels are shared with CRM records, not private account/invitation details.
        data["users"] = [await user_response(session, user)]
        owner_ids = select(Association.owner_id).union(
            select(Product.owner_id),
            select(FollowUp.owner_id),
            select(Activity.user_id).where(Activity.contact_id.is_not(None)),
        )
        owners = await session.scalars(
            select(User).where(User.id.in_(owner_ids), User.id != user.id).order_by(User.id)
        )
        data["users"].extend(
            {
                "id": owner.id,
                "firstName": owner.first_name,
                "lastName": owner.last_name,
                "email": "",
                "role": owner.role,
                "productIds": [],
                "active": owner.active,
            }
            for owner in owners
        )
    return data
