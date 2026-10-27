from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import ADMIN, has_permission
from app.core.security import decode_token
from app.db.models import Assignment, Association, User
from app.db.session import get_session

Session = Annotated[AsyncSession, Depends(get_session, scope="function")]
bearer = HTTPBearer(auto_error=False)


async def current_user(
    session: Session, auth: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
):
    try:
        payload = decode_token(auth.credentials if auth else "")
    except ValueError:
        raise HTTPException(401, "Invalid or expired access token", headers={"WWW-Authenticate": "Bearer"})
    user = await session.get(User, payload["sub"])
    if (
        not user
        or not user.active
        or user.deleted_at is not None
        or not user.password_set
        or user.token_version != payload["ver"]
    ):
        raise HTTPException(401, "Account or session is inactive")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


def require(permission):
    async def dependency(user: CurrentUser):
        if not has_permission(user.role, permission):
            raise HTTPException(403, "Insufficient permission")
        return user

    return dependency


async def assigned(session, user_id, product_id):
    return await session.get(Assignment, (user_id, product_id)) is not None


async def product_access(session, user, product_id):
    if user.role != ADMIN and not await assigned(session, user.id, product_id):
        raise HTTPException(404, "Product not found")


def association_scope(user):
    if user.role == ADMIN:
        return True
    return (Association.owner_id == user.id) & Association.product_id.in_(
        select(Assignment.product_id).where(Assignment.user_id == user.id)
    )


async def get_association(session, user, association_id, lock=False):
    query = select(Association).where(Association.id == association_id, association_scope(user))
    if lock:
        query = query.with_for_update()
    record = await session.scalar(query)
    if not record:
        raise HTTPException(404, "Relationship not found")
    return record


async def valid_owner(session, owner_id, product_id):
    owner = await session.get(User, owner_id)
    if (
        not owner
        or not owner.active
        or (owner.role != ADMIN and not await assigned(session, owner_id, product_id))
    ):
        raise HTTPException(422, "Owner must be active and assigned to this product")
    return owner
