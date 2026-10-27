"""Interactive bootstrap: python -m app.cli create-admin. No default credentials."""

import argparse
import asyncio
import getpass

from email_validator import validate_email
from sqlalchemy import select

from app.core.security import hash_password
from app.db.models import User
from app.db.session import SessionLocal, engine


async def create_admin():
    email = validate_email(input("Admin email: ").strip(), check_deliverability=False).normalized.lower()
    first_name = input("First name: ").strip()
    last_name = input("Last name: ").strip()
    password = getpass.getpass("Password (12–128 characters): ")
    confirm = getpass.getpass("Confirm password: ")
    if not first_name or not last_name or not 12 <= len(password) <= 128 or password != confirm:
        raise ValueError("Names are required and passwords must match and contain 12–128 characters")
    try:
        async with SessionLocal() as session, session.begin():
            if await session.scalar(select(User.id).where(User.email == email)):
                raise ValueError("Account already exists")
            session.add(
                User(
                    email=email,
                    first_name=first_name,
                    last_name=last_name,
                    password_hash=await asyncio.to_thread(hash_password, password),
                    role="Super Admin",
                )
            )
        print("Administrator created.")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["create-admin"])
    parser.parse_args()
    asyncio.run(create_admin())
