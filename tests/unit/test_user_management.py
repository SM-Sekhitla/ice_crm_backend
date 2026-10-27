"""Account-edit/deletion tests use fake DB sessions and never start services."""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

os.environ["DEFAULT_PLATFORM_ADMIN_EMAIL"] = ""
os.environ["DEFAULT_PLATFORM_ADMIN_PASSWORD"] = ""
os.environ.setdefault("SECRET_KEY", "test-only-secret-key-with-more-than-32-characters")

from app.api import admin
from app.api.schemas import UserUpdate
from app.db.models import Sender, User
from app.services import users


def account(user_id="staff", role="BDO"):
    return User(
        id=user_id,
        first_name="Example",
        last_name="User",
        email="old@example.com",
        role=role,
        active=True,
        password_set=True,
        onboarding_completed=True,
        deleted_at=None,
        token_version=3,
        password_hash="$argon2id$unchanged",
    )


def session():
    return SimpleNamespace(
        scalar=AsyncMock(),
        scalars=AsyncMock(),
        execute=AsyncMock(),
        flush=AsyncMock(),
        add=Mock(),
        add_all=Mock(),
    )


class UserEmailTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.record = account()
        self.actor = account("admin", "Super Admin")
        self.session = session()
        self.session.scalars.return_value = []
        self.session.scalar.side_effect = [self.record, None, None]
        self.body = UserUpdate(
            firstName="Example", lastName="User", email="NEW@EXAMPLE.COM", role="BDO", active=True
        )

    async def test_email_edit_normalizes_preserves_password_and_revokes_sessions(self):
        with (
            patch.object(admin, "validate_products", new_callable=AsyncMock),
            patch.object(admin, "user_response", new_callable=AsyncMock, return_value={}),
            patch.object(admin, "queue_invitation", new_callable=AsyncMock) as invite,
        ):
            await admin.update_user("staff", self.body, self.session, self.actor)
        self.assertEqual(self.record.email, "new@example.com")
        self.assertEqual(self.record.password_hash, "$argon2id$unchanged")
        self.assertEqual(self.record.token_version, 4)
        invite.assert_not_awaited()
        commands = [
            str(call.args[0].compile(dialect=postgresql.dialect()))
            for call in self.session.execute.call_args_list
        ]
        self.assertTrue(any("DELETE FROM refresh_tokens" in command for command in commands))
        self.assertTrue(any("UPDATE user_invitations" in command for command in commands))

    async def test_pending_email_edit_queues_new_link_for_corrected_address(self):
        self.record.password_set = False
        with (
            patch.object(admin, "validate_products", new_callable=AsyncMock),
            patch.object(admin, "user_response", new_callable=AsyncMock, return_value={}),
            patch.object(admin, "require_invitation_delivery", new_callable=AsyncMock) as require,
            patch.object(admin, "queue_invitation", new_callable=AsyncMock) as invite,
        ):
            await admin.update_user("staff", self.body, self.session, self.actor)
        require.assert_awaited_once_with(self.session)
        invite.assert_awaited_once_with(self.session, self.record, self.actor)
        self.assertEqual(self.record.email, "new@example.com")

    async def test_duplicate_email_rejected_before_account_changes(self):
        self.session.scalar.side_effect = [self.record, "existing-user"]
        with self.assertRaises(HTTPException) as error:
            await admin.update_user("staff", self.body, self.session, self.actor)
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.record.email, "old@example.com")
        self.session.execute.assert_not_awaited()

    async def test_existing_email_remains_when_older_client_omits_it(self):
        self.body.email = None
        self.session.scalar.side_effect = [self.record, None]
        with (
            patch.object(admin, "validate_products", new_callable=AsyncMock),
            patch.object(admin, "user_response", new_callable=AsyncMock, return_value={}),
        ):
            await admin.update_user("staff", self.body, self.session, self.actor)
        self.assertEqual(self.record.email, "old@example.com")

    async def test_deleted_accounts_cannot_be_edited(self):
        from app.core.security import now

        self.record.deleted_at = now()
        self.session.scalar.side_effect = [self.record]
        with self.assertRaises(HTTPException) as error:
            await admin.update_user("staff", self.body, self.session, self.actor)
        self.assertEqual(error.exception.status_code, 404)

    def test_invalid_email_is_rejected(self):
        with self.assertRaises(ValidationError):
            UserUpdate(firstName="Example", lastName="User", email="not-an-email", role="BDO", active=True)


class UserDeletionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.record = account()
        self.actor = account("admin", "Super Admin")
        self.session = session()
        self.session.scalar.side_effect = [self.record, self.actor]
        self.sender = Sender(id="sender", user_ids=["staff", "another-user"])
        self.session.scalars.side_effect = [
            [self.actor],
            ["p1"],
            ["p2"],
            ["p1"],
            [],
            ["scheduled-campaign"],
            [self.sender],
        ]

    async def test_deletion_removes_access_reassigns_work_and_preserves_history(self):
        await users.delete_user_account(self.session, self.actor, "staff", "admin")
        self.assertFalse(self.record.active)
        self.assertIsNotNone(self.record.deleted_at)
        self.assertEqual(self.record.password_hash, "!deleted")
        self.assertEqual(self.record.token_version, 4)
        self.assertEqual(self.record.email, "deleted-staff@users.invalid")
        self.assertEqual(self.record.first_name, "Example")
        self.assertEqual(self.sender.user_ids, ["another-user"])
        statements = [call.args[0] for call in self.session.execute.call_args_list]
        sql = [str(statement.compile(dialect=postgresql.dialect())) for statement in statements]
        for table in ["products", "associations", "follow_ups", "campaigns"]:
            self.assertTrue(any(f"UPDATE {table}" in item for item in sql), table)
        self.assertTrue(any("UPDATE campaign_recipients" in item for item in sql))
        self.assertTrue(any("DELETE FROM refresh_tokens" in item for item in sql))
        self.assertTrue(any("UPDATE user_invitations" in item for item in sql))
        self.assertFalse(
            any(
                item.startswith("DELETE FROM users")
                or item.startswith("DELETE FROM contacts")
                or item.startswith("DELETE FROM activities")
                for item in sql
            )
        )
        draft = next(
            statement
            for statement in statements
            if statement.table.name == "campaigns" and "created_by" in str(statement.compile())
        )
        values = draft.compile().params
        self.assertEqual(values["created_by"], "admin")
        self.assertIsNone(values["sender_id"])
        self.assertIsNone(values["signature_id"])
        self.session.flush.assert_awaited_once()

    async def test_self_deletion_is_blocked(self):
        self.session.scalar.side_effect = [self.actor]
        with self.assertRaises(HTTPException) as error:
            await users.delete_user_account(self.session, self.actor, "admin")
        self.assertEqual(error.exception.status_code, 409)
        self.session.execute.assert_not_awaited()

    async def test_last_active_admin_is_protected(self):
        self.record.role = "Super Admin"
        self.session.scalar.side_effect = [self.record, 1]
        with self.assertRaises(HTTPException) as error:
            await users.delete_user_account(self.session, self.actor, "staff")
        self.assertEqual(error.exception.status_code, 409)
        self.assertIn("last active", error.exception.detail)

    async def test_missing_or_previously_deleted_account_is_not_found(self):
        self.session.scalar.side_effect = [None]
        with self.assertRaises(HTTPException) as error:
            await users.delete_user_account(self.session, self.actor, "missing")
        self.assertEqual(error.exception.status_code, 404)

    async def test_inactive_or_pending_replacement_is_rejected(self):
        self.session.scalar.side_effect = [self.record, None]
        with self.assertRaises(HTTPException) as error:
            await users.delete_user_account(self.session, self.actor, "staff", "unavailable")
        self.assertEqual(error.exception.status_code, 422)
        self.session.execute.assert_not_awaited()

    async def test_replacement_cannot_be_the_user_being_deleted(self):
        self.session.scalar.side_effect = [self.record]
        with self.assertRaises(HTTPException) as error:
            await users.delete_user_account(self.session, self.actor, "staff", "staff")
        self.assertEqual(error.exception.status_code, 422)

    async def test_bdo_replacement_requires_existing_product_access(self):
        replacement = account("replacement")
        self.session.scalar.side_effect = [self.record, replacement]
        self.session.scalars.side_effect = [[], ["restricted-product"], [], [], [], []]
        with self.assertRaises(HTTPException) as error:
            await users.delete_user_account(self.session, self.actor, "staff", "replacement")
        self.assertEqual(error.exception.status_code, 409)
        self.session.execute.assert_not_awaited()
