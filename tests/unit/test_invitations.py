"""Invitation tests use fake sessions/transports; never start services or send email."""

import os
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException, Response
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

os.environ["DEFAULT_PLATFORM_ADMIN_EMAIL"] = ""
os.environ["DEFAULT_PLATFORM_ADMIN_PASSWORD"] = ""
os.environ.setdefault("SECRET_KEY", "test-only-secret-key-with-more-than-32-characters")

from app.api.schemas import InvitationAccept, UserCreate
from app.core.security import now, token_hash
from app.db.models import User, UserInvitation
from app.services import invitations
from app.services.email import DeliveryRejected


class Context:
    def __init__(self, value=None):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *args):
        return False


def pending_user():
    return User(
        id="pending-user",
        first_name="Example",
        last_name="User",
        email="pending@example.com",
        role="BDO",
        active=True,
        password_set=False,
        onboarding_completed=False,
        password_hash="unusable-random-hash",
        token_version=3,
    )


class InvitationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = SimpleNamespace(
            secret_key="test-only-secret-key-with-more-than-32-characters",
            frontend_base_url="http://localhost:5173",
            invitation_expiry_hours=24,
            email_delivery_enabled=True,
            email_provider="brevo",
            brevo_from_email="sender@example.com",
            brevo_from_name="ICE",
            smtp_from_email="",
            smtp_from_name="ICE",
        )
        self.session = SimpleNamespace(
            execute=AsyncMock(),
            scalar=AsyncMock(),
            get=AsyncMock(),
            flush=AsyncMock(),
            add=Mock(),
            begin=Mock(return_value=Context()),
        )
        self.factory = Mock(return_value=Context(self.session))
        self.user = pending_user()
        self.record = UserInvitation(
            id="invitation",
            user_id=self.user.id,
            created_by="admin",
            digest=token_hash("a" * 64),
            encrypted_token=None,
            expires_at=now() + timedelta(hours=24),
            revoked=False,
            used_at=None,
            delivery_status="Queued",
        )
        self.patches = [
            patch.object(invitations, "get_settings", return_value=self.settings),
            patch.object(invitations, "SessionLocal", self.factory),
        ]
        for item in self.patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in reversed(self.patches)])

    async def test_queue_stores_hash_and_encrypted_outbox_token(self):
        record = await invitations.queue_invitation(self.session, self.user, SimpleNamespace(id="admin"))
        raw = invitations.token_cipher().decrypt(record.encrypted_token.encode()).decode()
        self.assertEqual(record.digest, token_hash(raw))
        from app.services.common import serialize

        exported = serialize(record)
        self.assertNotIn("encryptedToken", exported)
        self.assertNotIn("digest", exported)
        self.assertNotEqual(raw, record.encrypted_token)
        self.assertNotIn(raw, record.encrypted_token)
        self.assertGreater(record.expires_at, now())
        self.assertLessEqual(record.expires_at, now() + timedelta(hours=24))
        self.assertIn("/activate#token=", invitations.invitation_url(raw))
        self.session.execute.assert_awaited_once()  # old links revoked

    async def test_verify_is_non_consuming(self):
        self.session.scalar.return_value = self.record
        self.session.get.return_value = self.user
        await invitations.verify_invitation(self.session, "a" * 64)
        self.assertIsNone(self.record.used_at)
        self.session.flush.assert_not_awaited()

    async def test_expired_revoked_and_used_tokens_are_rejected(self):
        for changes in [{"expires_at": now() - timedelta(seconds=1)}, {"revoked": True}, {"used_at": now()}]:
            with self.subTest(changes=changes):
                self.record.expires_at = now() + timedelta(hours=1)
                self.record.revoked = False
                self.record.used_at = None
                for key, value in changes.items():
                    setattr(self.record, key, value)
                self.session.scalar.return_value = self.record
                with self.assertRaises(HTTPException) as error:
                    await invitations.find_invitation(self.session, "a" * 64)
                self.assertEqual(error.exception.status_code, 400)

    async def test_inactive_and_already_activated_accounts_cannot_claim(self):
        for active, password_set in [(False, False), (True, True)]:
            self.user.active = active
            self.user.password_set = password_set
            self.session.scalar.return_value = self.record
            self.session.get.return_value = self.user
            with self.assertRaises(HTTPException):
                await invitations.verify_invitation(self.session, "a" * 64)

    async def test_accept_sets_password_once_preserves_role_and_revokes_old_sessions(self):
        self.session.scalar.side_effect = [self.record, self.user, self.record]
        with patch.object(invitations, "hash_password", return_value="$argon2id$new-hash") as hash_password:
            user = await invitations.accept_invitation(self.session, "a" * 64, "UniquePassword2026!")
            hash_password.assert_called_once_with("UniquePassword2026!")
        self.assertTrue(user.password_set)
        self.assertFalse(user.onboarding_completed)
        self.assertEqual(user.role, "BDO")
        self.assertEqual(user.token_version, 4)
        self.assertEqual(user.password_hash, "$argon2id$new-hash")
        self.assertIsNotNone(self.record.used_at)
        self.assertIsNone(self.record.encrypted_token)
        self.assertEqual(self.session.execute.await_count, 2)
        self.session.scalar.side_effect = None
        self.session.scalar.return_value = self.record
        with self.assertRaises(HTTPException):
            await invitations.accept_invitation(self.session, "a" * 64, "AnotherPassword2026!")

    async def test_locked_record_is_rechecked_after_a_concurrent_claim(self):
        used = UserInvitation(
            id="invitation", used_at=now(), revoked=False, expires_at=now() + timedelta(hours=1)
        )
        self.session.scalar.side_effect = [self.record, self.user, used]
        with patch.object(invitations, "hash_password") as hash_password:
            with self.assertRaises(HTTPException):
                await invitations.accept_invitation(self.session, "a" * 64, "UniquePassword2026!")
            hash_password.assert_not_called()

    async def test_creation_is_blocked_when_real_email_is_disabled(self):
        self.settings.email_delivery_enabled = False
        with self.assertRaises(HTTPException) as error:
            await invitations.require_invitation_delivery(self.session)
        self.assertEqual(error.exception.status_code, 409)

    async def test_worker_stays_idle_when_email_is_disabled(self):
        self.settings.email_delivery_enabled = False
        self.assertFalse(await invitations.process_invitation())
        self.factory.assert_not_called()

    async def test_worker_sends_invitation_and_erases_outbox_token(self):
        self.record.encrypted_token = invitations.token_cipher().encrypt(("a" * 64).encode()).decode()
        self.session.scalar.side_effect = [self.user, self.record]
        self.session.get.return_value = self.record
        send = AsyncMock(return_value="<provider-message>")
        with patch.object(invitations, "brevo_send", send):
            self.assertTrue(await invitations.process_invitation())
        send.assert_awaited_once()
        self.assertIn("/activate#token=", send.call_args.args[3])
        self.assertEqual(self.record.delivery_status, "Accepted")
        self.assertIsNone(self.record.encrypted_token)
        self.assertNotIn("a" * 64, self.record.delivery_reason)

    async def test_workspace_returns_invitation_status_without_loading_tokens(self):
        from app.api import workspace

        admin = SimpleNamespace(id="admin", role="Super Admin")

        async def scalars(query):
            entity = query.column_descriptions[0].get("entity")
            return [admin, self.user] if entity is User else []

        self.session.commit = AsyncMock()
        self.session.scalars = AsyncMock(side_effect=scalars)
        row = SimpleNamespace(
            user_id=self.user.id,
            delivery_status="Queued",
            delivery_reason="",
            expires_at=self.record.expires_at,
        )
        self.session.execute.side_effect = [None, [row]]

        async def profile(session, person):
            return {"id": person.id, "passwordSet": person.id == "admin"}

        with patch.object(workspace, "user_response", side_effect=profile):
            data = await workspace.workspace(self.session, admin)
        invited = next(person for person in data["users"] if person["id"] == self.user.id)
        self.assertEqual(invited["invitationStatus"], "Queued")
        query = self.session.execute.call_args.args[0].compile(dialect=postgresql.dialect())
        self.assertNotIn("encrypted_token", str(query))
        self.assertNotIn("digest", str(query))

    async def test_smtp_invitation_uses_threaded_transport(self):
        from app.services import worker

        self.settings.email_provider = "smtp"
        self.settings.smtp_from_email = "sender@example.com"
        self.record.encrypted_token = invitations.token_cipher().encrypt(("a" * 64).encode()).decode()
        self.session.scalar.side_effect = [self.user, self.record]
        self.session.get.return_value = self.record
        with (
            patch.object(worker, "smtp_send") as send,
            patch.object(invitations, "brevo_send", new_callable=AsyncMock) as brevo,
        ):
            self.assertTrue(await invitations.process_invitation())
        send.assert_called_once()
        brevo.assert_not_awaited()
        self.assertEqual(self.record.delivery_status, "Accepted")

    async def test_worker_outcomes_do_not_blindly_retry(self):
        for outcome, expected in [
            (DeliveryRejected("rejected"), "Failed"),
            (TimeoutError("uncertain"), "Unknown"),
        ]:
            self.record.delivery_status = "Queued"
            self.record.encrypted_token = invitations.token_cipher().encrypt(("a" * 64).encode()).decode()
            self.session.scalar.side_effect = [self.user, self.record]
            self.session.get.return_value = self.record
            send = AsyncMock(side_effect=outcome)
            with patch.object(invitations, "brevo_send", send):
                await invitations.process_invitation()
            send.assert_awaited_once()
            self.assertEqual(self.record.delivery_status, expected)
            self.assertIsNone(self.record.encrypted_token)

    async def test_revoked_invitation_is_cancelled_without_sending(self):
        self.record.revoked = True
        self.session.scalar.side_effect = [self.user, self.record]
        send = AsyncMock()
        with patch.object(invitations, "brevo_send", send):
            await invitations.process_invitation()
        send.assert_not_awaited()
        self.assertEqual(self.record.delivery_status, "Cancelled")

    async def test_pending_user_cannot_login_even_if_password_verification_succeeds(self):
        from app.api import auth
        from app.api.schemas import Login

        self.session.scalar.return_value = self.user
        with (
            patch.object(auth, "verify_password", return_value=True),
            patch.object(auth, "issue", new_callable=AsyncMock) as issue,
        ):
            with self.assertRaises(HTTPException) as error:
                await auth.login(
                    Login(email=self.user.email, password="UnknownPassword2026!"), self.session, Response()
                )
            self.assertEqual(error.exception.status_code, 401)
            issue.assert_not_awaited()

    async def test_admin_creation_queues_invitation_instead_of_accepting_password(self):
        from app.api import admin

        body = UserCreate(firstName="Example", lastName="User", email="pending@example.com")
        actor = SimpleNamespace(id="admin")
        with (
            patch.object(admin, "require_invitation_delivery", new_callable=AsyncMock),
            patch.object(admin, "validate_products", new_callable=AsyncMock),
            patch.object(admin, "hash_password", return_value="$argon2id$random"),
            patch.object(admin, "queue_invitation", new_callable=AsyncMock) as queued,
            patch.object(admin, "user_response", new_callable=AsyncMock, return_value={"passwordSet": False}),
        ):
            result = await admin.create_user(body, self.session, actor)
        created = self.session.add.call_args_list[0].args[0]
        self.assertFalse(created.password_set)
        self.assertFalse(created.onboarding_completed)
        self.assertEqual(created.role, "BDO")
        self.assertEqual(result, {"passwordSet": False})
        queued.assert_awaited_once_with(self.session, created, actor)

    async def test_resend_rejects_already_activated_accounts(self):
        from app.api import admin

        self.user.password_set = True
        self.session.scalar.return_value = self.user
        with (
            patch.object(admin, "require_invitation_delivery", new_callable=AsyncMock),
            patch.object(admin, "queue_invitation", new_callable=AsyncMock) as queued,
        ):
            with self.assertRaises(HTTPException) as error:
                await admin.resend_invitation(self.user.id, self.session, SimpleNamespace(id="admin"))
        self.assertEqual(error.exception.status_code, 409)
        queued.assert_not_awaited()

    async def test_onboarding_completion_is_server_backed(self):
        from app.api.auth import complete_onboarding

        self.user.password_set = True
        await complete_onboarding(self.user, self.session)
        self.assertTrue(self.user.onboarding_completed)


class InvitationSchemaTests(unittest.TestCase):
    def test_admin_does_not_supply_an_initial_password(self):
        body = {"firstName": "Example", "lastName": "User", "email": "pending@example.com", "role": "BDO"}
        self.assertEqual(UserCreate(**body).role, "BDO")
        with self.assertRaises(ValidationError):
            UserCreate(**body, password="NoInitialPassword2026!")

    def test_acceptance_requires_matching_password_fields(self):
        with self.assertRaises(ValidationError):
            InvitationAccept(
                token="a" * 64, password="NewPassword2026!", confirmPassword="DifferentPassword2026!"
            )
        valid = InvitationAccept(
            token="a" * 64, password="NewPassword2026!", confirmPassword="NewPassword2026!"
        )
        self.assertEqual(valid.password, valid.confirm_password)
