"""Create the configured initial administrator without resetting managed accounts."""

import asyncio
import logging

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert

from app.core.config import get_settings
from app.core.permissions import ADMIN
from app.core.security import hash_password
from app.db.models import Activity, User
from app.db.session import SessionLocal

logger = logging.getLogger("crm.bootstrap")
# A transaction-scoped PostgreSQL lock serializes reloads and multiple API workers.
ADMIN_BOOTSTRAP_LOCK = 0x49434543524D4144


async def ensure_initial_admin():
    settings = get_settings()
    email = settings.default_platform_admin_email
    if not email:
        return False
    async with SessionLocal() as session, session.begin():
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": ADMIN_BOOTSTRAP_LOCK}
        )
        existing = await session.scalar(select(User.id).where(func.lower(User.email) == email))
        if existing:
            logger.info("Configured initial administrator account exists; no account changes applied")
            return False
        # This is an initial bootstrap, not an account synchronizer. Email edits/deletions
        # must not recreate the old administrator identity on the next reload.
        administrator = await session.scalar(
            select(User.id)
            .where(
                User.role == ADMIN,
                User.active.is_(True),
                User.password_set.is_(True),
                User.deleted_at.is_(None),
            )
            .limit(1)
        )
        if administrator:
            return False
        password = settings.default_platform_admin_password.get_secret_value()
        if not password:
            raise RuntimeError(
                "Set DEFAULT_PLATFORM_ADMIN_PASSWORD to create the configured initial Super Admin "
                "(12–128 characters). Existing accounts are never reset during startup."
            )
        name = settings.default_platform_admin_username.strip() or "Platform Admin"
        parts = name.split(maxsplit=1)
        first_name, last_name = parts[0][:100], (parts[1] if len(parts) > 1 else "Admin")[:100]
        encoded = await asyncio.to_thread(hash_password, password)
        # The unique constraint also protects against account creation by another API request.
        user_id = await session.scalar(
            insert(User)
            .values(
                first_name=first_name,
                last_name=last_name,
                email=email,
                password_hash=encoded,
                role=ADMIN,
                active=True,
                token_version=0,
            )
            .on_conflict_do_nothing(index_elements=[User.email])
            .returning(User.id)
        )
        if not user_id:
            return False
        session.add(Activity(user_id=user_id, description="Initial Super Admin created during startup"))
    logger.info("Initial Super Admin created from configured bootstrap settings")
    return True
