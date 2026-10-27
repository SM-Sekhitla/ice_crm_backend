import os

os.environ.setdefault("SECRET_KEY", "test-only-secret-key-with-more-than-32-characters")
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://hlaks@127.0.0.1:55439/crm_test"
)
os.environ["AUTH_RATE_LIMIT"] = "10000"
if not os.environ["DATABASE_URL"].endswith("/crm_test"):
    raise RuntimeError("Tests require a dedicated database named crm_test")

import httpx
import pytest_asyncio
from sqlalchemy import text

from app.core.security import hash_password
from app.db.models import Assignment, Product, User
from app.db.session import SessionLocal
from app.main import app

PASSWORD = "TestPassword2026!"
PASSWORD_HASH = hash_password(PASSWORD)


@pytest_asyncio.fixture(autouse=True)
async def clean_database():
    async with SessionLocal() as session, session.begin():
        await session.execute(text("TRUNCATE users, products, contacts, senders RESTART IDENTITY CASCADE"))
        session.add_all(
            [
                User(
                    id="admin",
                    first_name="Admin",
                    last_name="Example",
                    email="admin@example.com",
                    password_hash=PASSWORD_HASH,
                    role="Super Admin",
                ),
                User(
                    id="bdo",
                    first_name="BDO",
                    last_name="Example",
                    email="bdo@example.com",
                    password_hash=PASSWORD_HASH,
                    role="BDO",
                ),
                User(
                    id="other",
                    first_name="Other",
                    last_name="Example",
                    email="other@example.com",
                    password_hash=PASSWORD_HASH,
                    role="BDO",
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                Product(id="p1", name="Product One", code="ONE", type="Product", owner_id="admin"),
                Product(id="p2", name="Product Two", code="TWO", type="Product", owner_id="admin"),
            ]
        )
        await session.flush()
        session.add_all(
            [Assignment(user_id="bdo", product_id="p1"), Assignment(user_id="other", product_id="p2")]
        )


@pytest_asyncio.fixture
async def client():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        yield client


async def login(client, email="admin@example.com"):
    response = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()


async def headers(client, email="admin@example.com"):
    tokens = await login(client, email)
    return {"Authorization": f"Bearer {tokens['accessToken']}"}


async def contact(client, auth, email="contact@example.com", product="p1", owner="bdo", consent=True):
    response = await client.post(
        "/api/v1/contacts",
        headers=auth,
        json={
            "firstName": "Example",
            "lastName": "Contact",
            "email": email,
            "productIds": [product],
            "ownerId": owner,
            "consent": consent,
            "consentSource": "Written opt-in" if consent else "",
            "consentDate": "2026-01-01" if consent else "",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()
