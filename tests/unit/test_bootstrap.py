"""Run without starting services: python -m unittest discover -s tests/unit -v."""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from pydantic import SecretStr, ValidationError
from sqlalchemy.dialects import postgresql

# Isolate tests from a developer's bootstrap credentials. No database is contacted.
os.environ["DEFAULT_PLATFORM_ADMIN_EMAIL"] = ""
os.environ["DEFAULT_PLATFORM_ADMIN_PASSWORD"] = ""
os.environ.setdefault("SECRET_KEY", "test-only-secret-key-with-more-than-32-characters")

from app.core.config import Settings
from app.services import bootstrap


class Context:
    def __init__(self, value=None):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *args):
        return False


class BootstrapTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = SimpleNamespace(
            default_platform_admin_email="initial@example.com",
            default_platform_admin_password=SecretStr("UniqueInitialPassword2026!"),
            default_platform_admin_username="Initial Administrator",
        )
        self.session = SimpleNamespace(
            execute=AsyncMock(), scalar=AsyncMock(), add=Mock(), begin=Mock(return_value=Context())
        )
        self.factory = Mock(return_value=Context(self.session))
        self.patches = [
            patch.object(bootstrap, "get_settings", return_value=self.settings),
            patch.object(bootstrap, "SessionLocal", self.factory),
            patch.object(bootstrap, "hash_password", return_value="$argon2id$test-encoded"),
        ]
        self.started = [item.start() for item in self.patches]
        self.addCleanup(lambda: [item.stop() for item in reversed(self.patches)])

    async def test_missing_email_skips_bootstrap(self):
        self.settings.default_platform_admin_email = ""
        self.assertFalse(await bootstrap.ensure_initial_admin())
        self.factory.assert_not_called()

    async def test_creates_active_super_admin_with_hashed_password_and_audit(self):
        self.session.scalar.side_effect = [None, None, "new-admin-id"]
        self.assertTrue(await bootstrap.ensure_initial_admin())
        self.session.execute.assert_awaited_once()
        query = self.session.scalar.call_args_list[2].args[0].compile(dialect=postgresql.dialect())
        self.assertIn("ON CONFLICT", str(query))
        self.assertEqual(query.params["role"], "Super Admin")
        self.assertTrue(query.params["active"])
        self.assertEqual(query.params["email"], "initial@example.com")
        self.assertEqual(query.params["first_name"], "Initial")
        self.assertEqual(query.params["last_name"], "Administrator")
        self.assertEqual(query.params["password_hash"], "$argon2id$test-encoded")
        self.assertNotEqual(
            query.params["password_hash"], self.settings.default_platform_admin_password.get_secret_value()
        )
        audit = self.session.add.call_args.args[0]
        self.assertEqual(audit.user_id, "new-admin-id")
        self.assertNotIn("UniqueInitialPassword", audit.description)

    async def test_restart_does_not_reset_or_duplicate_existing_account(self):
        self.session.scalar.side_effect = [None, None, "new-admin-id", "new-admin-id"]
        self.assertTrue(await bootstrap.ensure_initial_admin())
        self.settings.default_platform_admin_password = SecretStr("ChangedConfiguredPassword!")
        self.assertFalse(await bootstrap.ensure_initial_admin())
        self.started[2].assert_called_once_with("UniqueInitialPassword2026!")
        self.session.add.assert_called_once()

    async def test_existing_identity_is_not_promoted_or_reactivated(self):
        self.session.scalar.return_value = "existing-managed-user"
        self.settings.default_platform_admin_password = SecretStr("")
        self.assertFalse(await bootstrap.ensure_initial_admin())
        self.session.add.assert_not_called()
        self.started[2].assert_not_called()
        self.session.scalar.assert_awaited_once()

    async def test_missing_password_prevents_creation(self):
        self.session.scalar.return_value = None
        self.settings.default_platform_admin_password = SecretStr("")
        with self.assertRaisesRegex(RuntimeError, "DEFAULT_PLATFORM_ADMIN_PASSWORD"):
            await bootstrap.ensure_initial_admin()
        self.session.add.assert_not_called()
        self.started[2].assert_not_called()

    async def test_concurrent_insert_conflict_never_overwrites_existing_account(self):
        self.session.scalar.side_effect = [None, None, None]
        self.assertFalse(await bootstrap.ensure_initial_admin())
        self.session.add.assert_not_called()

    async def test_bootstrap_email_change_does_not_recreate_an_old_admin_identity(self):
        self.session.scalar.side_effect = [None, "another-active-admin"]
        self.assertFalse(await bootstrap.ensure_initial_admin())
        self.started[2].assert_not_called()
        self.session.add.assert_not_called()

    async def test_lifespan_bootstraps_before_serving(self):
        from app import main

        dispose = AsyncMock()
        with (
            patch.object(main, "ensure_initial_admin", new_callable=AsyncMock) as ensure,
            patch.object(main, "engine", SimpleNamespace(dispose=dispose)),
            patch.object(main, "record_backend_log", new_callable=AsyncMock),
            patch.object(main, "RuntimeLogCapture", return_value=Context()),
        ):
            async with main.lifespan(main.app):
                ensure.assert_awaited_once()
                dispose.assert_not_awaited()
            dispose.assert_awaited_once()


class BootstrapConfigurationTests(unittest.TestCase):
    def test_email_normalization_and_secret_redaction(self):
        config = Settings(
            _env_file=None,
            secret_key="test-only-secret-key-with-more-than-32-characters",
            default_platform_admin_email=" INITIAL@EXAMPLE.COM ",
            default_platform_admin_password="UniqueInitialPassword2026!",
        )
        self.assertEqual(config.default_platform_admin_email, "initial@example.com")
        self.assertNotIn("UniqueInitialPassword2026!", repr(config))

    def test_weak_initial_password_is_rejected(self):
        with self.assertRaises(ValidationError):
            Settings(
                _env_file=None,
                secret_key="test-only-secret-key-with-more-than-32-characters",
                default_platform_admin_email="initial@example.com",
                default_platform_admin_password="short",
            )
