import asyncio
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Response
from sqlalchemy import select, update

from app.api.deps import CurrentUser, Session
from app.api.schemas import Login, PasswordChange, Refresh
from app.core.config import get_settings
from app.core.security import access_token, hash_password, now, opaque_token, token_hash, verify_password
from app.db.models import Assignment, RefreshToken, User
from app.services.common import audit, serialize

router = APIRouter(prefix="/auth", tags=["Authentication"])
# Equal-cost verification for missing accounts to reduce account enumeration.
DUMMY_HASH = hash_password("unused-random-dummy-credential")


async def user_response(session, user):
    data = serialize(user)
    data["productIds"] = list(
        await session.scalars(select(Assignment.product_id).where(Assignment.user_id == user.id))
    )
    return data


async def issue(session, user):
    raw = opaque_token()
    session.add(
        RefreshToken(
            user_id=user.id,
            digest=token_hash(raw),
            expires_at=now() + timedelta(days=get_settings().refresh_token_days),
        )
    )
    return {
        "accessToken": access_token(user),
        "refreshToken": raw,
        "tokenType": "bearer",
        "expiresIn": get_settings().access_token_minutes * 60,
        "user": await user_response(session, user),
    }


@router.post("/login")
async def login(body: Login, session: Session, response: Response):
    response.headers["Cache-Control"] = "no-store"
    user = await session.scalar(select(User).where(User.email == str(body.email).lower()))
    valid = await asyncio.to_thread(
        verify_password, body.password, user.password_hash if user else DUMMY_HASH
    )
    if not valid or not user or not user.active:
        raise HTTPException(401, "Invalid email or password")
    audit(session, user, "Signed in")
    return await issue(session, user)


@router.post("/refresh")
async def refresh(body: Refresh, session: Session, response: Response):
    response.headers["Cache-Control"] = "no-store"
    token = await session.scalar(
        select(RefreshToken).where(RefreshToken.digest == token_hash(body.refresh_token)).with_for_update()
    )
    if not token or token.revoked or token.expires_at <= now():
        raise HTTPException(401, "Invalid or expired refresh token")
    user = await session.get(User, token.user_id)
    if not user or not user.active:
        raise HTTPException(401, "Inactive account")
    token.revoked = True
    return await issue(session, user)


@router.post("/logout", status_code=204)
async def logout(user: CurrentUser, session: Session):
    user.token_version += 1
    await session.execute(update(RefreshToken).where(RefreshToken.user_id == user.id).values(revoked=True))
    audit(session, user, "Signed out of all sessions")


@router.get("/me")
async def me(user: CurrentUser, session: Session):
    return await user_response(session, user)


@router.post("/password", status_code=204)
async def password(body: PasswordChange, user: CurrentUser, session: Session):
    if not await asyncio.to_thread(verify_password, body.current_password, user.password_hash):
        raise HTTPException(400, "Current password is incorrect")
    user.password_hash = await asyncio.to_thread(hash_password, body.password)
    user.token_version += 1
    await session.execute(update(RefreshToken).where(RefreshToken.user_id == user.id).values(revoked=True))
    audit(session, user, "Changed password; sessions revoked")
