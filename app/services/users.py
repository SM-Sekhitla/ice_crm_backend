"""Remove login access while retaining audit authorship and CRM history."""

from fastapi import HTTPException
from sqlalchemy import case, delete, func, select, update

from app.core.permissions import ADMIN
from app.core.security import now
from app.db.models import (
    Assignment,
    Association,
    AudienceSegment,
    Campaign,
    FollowUp,
    Product,
    Recipient,
    RefreshToken,
    Sender,
    User,
    UserInvitation,
)
from app.services.common import audit


async def delete_user_account(session, actor, user_id, replacement_id=None):
    # Match account-edit locking, including concurrent administrator deletions.
    await session.scalars(select(User).where(User.role == ADMIN).order_by(User.id).with_for_update())
    record = await session.scalar(
        select(User).where(User.id == user_id, User.deleted_at.is_(None)).with_for_update()
    )
    if not record:
        raise HTTPException(404, "User not found")
    if record.id == actor.id:
        raise HTTPException(409, "You cannot delete your own account")
    if record.role == ADMIN and record.active:
        remaining = await session.scalar(
            select(func.count())
            .select_from(User)
            .where(
                User.role == ADMIN,
                User.active.is_(True),
                User.password_set.is_(True),
                User.deleted_at.is_(None),
            )
        )
        if remaining <= 1:
            raise HTTPException(409, "The last active Super Admin cannot be deleted")
    replacement_id = replacement_id or actor.id
    if replacement_id == record.id:
        raise HTTPException(422, "Choose another user to take over this account's work")
    replacement = await session.scalar(
        select(User)
        .where(
            User.id == replacement_id,
            User.active.is_(True),
            User.password_set.is_(True),
            User.deleted_at.is_(None),
        )
        .with_for_update()
    )
    if not replacement:
        raise HTTPException(422, "Choose an active user who has completed account setup")
    required_products = set(await session.scalars(select(Product.id).where(Product.owner_id == record.id)))
    required_products.update(
        await session.scalars(select(Association.product_id).where(Association.owner_id == record.id))
    )
    required_products.update(
        await session.scalars(
            select(Campaign.product_id).where(Campaign.created_by == record.id, Campaign.status == "Draft")
        )
    )
    required_products.update(
        await session.scalars(
            select(Association.product_id)
            .join(FollowUp, FollowUp.association_id == Association.id)
            .where(FollowUp.owner_id == record.id, FollowUp.status == "Open")
        )
    )
    if replacement.role != ADMIN:
        assigned = set(
            await session.scalars(select(Assignment.product_id).where(Assignment.user_id == replacement.id))
        )
        if required_products - assigned:
            raise HTTPException(
                409, "The replacement owner must already have access to every affected product"
            )
    # Lock submitted campaigns before changing queues, using the same order as the worker.
    cancelled_ids = list(
        await session.scalars(
            select(Campaign.id)
            .where(Campaign.created_by == record.id, Campaign.status.in_(["Scheduled", "Sending"]))
            .order_by(Campaign.created_at)
            .with_for_update()
        )
    )
    await session.execute(
        update(Product).where(Product.owner_id == record.id).values(owner_id=replacement.id)
    )
    await session.execute(
        update(Association).where(Association.owner_id == record.id).values(owner_id=replacement.id)
    )
    await session.execute(
        update(FollowUp)
        .where(FollowUp.owner_id == record.id, FollowUp.status == "Open")
        .values(owner_id=replacement.id)
    )
    # Reassigned drafts need an explicit review of their new sender/signature.
    draft_values = {
        "created_by": replacement.id,
        "sender_id": None,
        "signature_id": None,
        "signature_html": "",
    }
    await session.execute(
        update(Campaign)
        .where(Campaign.created_by == record.id, Campaign.status == "Draft")
        .values(**draft_values)
    )
    if cancelled_ids:
        await session.execute(
            update(Recipient)
            .where(Recipient.campaign_id.in_(cancelled_ids), Recipient.status == "Queued")
            .values(status="Excluded", reason="Campaign owner account deleted", processed_at=now())
        )
        await session.execute(
            update(Campaign).where(Campaign.id.in_(cancelled_ids)).values(status="Cancelled")
        )
    for sender in await session.scalars(
        select(Sender).where(Sender.user_ids.contains([record.id])).with_for_update()
    ):
        sender.user_ids = [uid for uid in sender.user_ids if uid != record.id]
    await session.execute(delete(AudienceSegment).where(AudienceSegment.created_by == record.id))
    await session.execute(delete(Assignment).where(Assignment.user_id == record.id))
    await session.execute(delete(RefreshToken).where(RefreshToken.user_id == record.id))
    await session.execute(
        update(UserInvitation)
        .where(UserInvitation.user_id == record.id)
        .values(
            revoked=True,
            encrypted_token=None,
            delivery_status=case(
                (UserInvitation.delivery_status == "Queued", "Cancelled"),
                else_=UserInvitation.delivery_status,
            ),
            delivery_reason=case(
                (UserInvitation.delivery_status == "Queued", "Account deleted"),
                else_=UserInvitation.delivery_reason,
            ),
        )
    )
    record.active = False
    record.deleted_at = now()
    record.token_version += 1
    record.password_hash = "!deleted"
    # Free the old address for a new invitation without reusing the deleted user's ID/history.
    record.email = f"deleted-{record.id}@users.invalid"
    audit(
        session, actor, f"Deleted user account {record.first_name} {record.last_name}; ownership reassigned"
    )
    await session.flush()
