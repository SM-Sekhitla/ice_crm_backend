import asyncio

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select

from app.api.auth import user_response
from app.api.deps import Session, require
from app.api.schemas import ProductInput, UserCreate, UserUpdate
from app.core.permissions import ADMIN
from app.core.security import hash_password
from app.db.models import Assignment, Association, Campaign, Product, RefreshToken, User
from app.services.common import audit, serialize

router = APIRouter(tags=["Administration"])


async def validate_products(session, ids):
    for pid in set(ids):
        if not await session.get(Product, pid):
            raise HTTPException(422, "Unknown product assignment")


@router.get("/users")
async def users(session: Session, user=Depends(require("users.manage"))):
    return [
        await user_response(session, record)
        for record in await session.scalars(select(User).order_by(User.email))
    ]


@router.post("/users", status_code=201)
async def create_user(body: UserCreate, session: Session, user=Depends(require("users.manage"))):
    await validate_products(session, body.product_ids)
    record = User(
        **body.model_dump(exclude={"password", "product_ids", "email"}),
        email=str(body.email).lower(),
        password_hash=await asyncio.to_thread(hash_password, body.password),
    )
    session.add(record)
    await session.flush()
    for pid in set(body.product_ids):
        session.add(Assignment(user_id=record.id, product_id=pid))
    await session.flush()
    audit(session, user, "Created user account")
    return await user_response(session, record)


@router.put("/users/{user_id}")
async def update_user(
    user_id: str, body: UserUpdate, session: Session, user=Depends(require("users.manage"))
):
    # Serialize administrator membership changes, preserving at least one active admin.
    await session.scalars(select(User).where(User.role == ADMIN).order_by(User.id).with_for_update())
    record = await session.get(User, user_id, populate_existing=True)
    if not record:
        raise HTTPException(404, "User not found")
    if record.id == user.id and (body.role != ADMIN or not body.active):
        raise HTTPException(409, "Cannot deactivate or demote your own administrator account")
    await validate_products(session, body.product_ids)
    # Removing an assignment requires relationships to be reassigned first.
    if body.role != ADMIN:
        invalid = await session.scalar(
            select(Association.id)
            .where(Association.owner_id == record.id, Association.product_id.not_in(body.product_ids))
            .limit(1)
        )
        if invalid:
            raise HTTPException(409, "Reassign owned relationships before removing product access")
    for field, value in body.model_dump(exclude={"product_ids"}).items():
        setattr(record, field, value)
    record.token_version += 1
    await session.execute(delete(RefreshToken).where(RefreshToken.user_id == record.id))
    await session.execute(delete(Assignment).where(Assignment.user_id == record.id))
    session.add_all([Assignment(user_id=record.id, product_id=pid) for pid in set(body.product_ids)])
    await session.flush()
    audit(session, user, "Updated user permissions and revoked sessions")
    return await user_response(session, record)


async def valid_product_owner(session, owner_id):
    owner = await session.get(User, owner_id)
    if not owner or not owner.active:
        raise HTTPException(422, "Product owner must be an active user")


@router.post("/products", status_code=201)
async def create_product(body: ProductInput, session: Session, user=Depends(require("products.manage"))):
    await valid_product_owner(session, body.owner_id)
    record = Product(**body.model_dump(exclude={"code"}), code=body.code.upper())
    session.add(record)
    await session.flush()
    session.add(Assignment(user_id=body.owner_id, product_id=record.id))
    audit(session, user, "Created product", product_id=record.id)
    return serialize(record)


@router.put("/products/{product_id}")
async def update_product(
    product_id: str, body: ProductInput, session: Session, user=Depends(require("products.manage"))
):
    record = await session.scalar(select(Product).where(Product.id == product_id).with_for_update())
    if not record:
        raise HTTPException(404, "Product not found")
    await valid_product_owner(session, body.owner_id)
    for field, value in body.model_dump().items():
        setattr(record, field, value.upper() if field == "code" else value)
    if not await session.get(Assignment, (body.owner_id, product_id)):
        session.add(Assignment(user_id=body.owner_id, product_id=product_id))
    audit(session, user, "Updated product", product_id=product_id)
    await session.flush()
    return serialize(record)


@router.delete("/products/{product_id}", status_code=204)
async def delete_product(product_id: str, session: Session, user=Depends(require("products.manage"))):
    record = await session.get(Product, product_id)
    if not record:
        raise HTTPException(404, "Product not found")
    for model in (Association, Campaign):
        if await session.scalar(select(model.id).where(model.product_id == product_id).limit(1)):
            raise HTTPException(409, "Product has CRM history; archive it instead")
    # Other references (segments) are protected by FK constraints.
    audit(session, user, f"Deleted unused product {record.code}")
    await session.delete(record)
