"""Runtime logging checks are isolated from databases, providers and services."""

import logging
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("SECRET_KEY", "test-only-secret-key-with-more-than-32-characters")

from pydantic import SecretStr

from app.services import runtime_logging
from app.services.backend_logs import LOG_WRITE_IN_PROGRESS
from app.services.runtime_logging import LogRedactor, RuntimeLogCapture, RuntimeLogHandler


class RedactionTests(unittest.TestCase):
    def test_known_keys_passwords_tokens_urls_emails_and_payloads_are_removed(self):
        clean = LogRedactor(["configured-secret-value"])
        cases = [
            ("configured-secret-value", "configured-secret-value"),
            ('password="simple secret"', "simple secret"),
            ("SMTP_PASSWORD=short-value", "short-value"),
            ("<html><body>private content</body></html>", "private content"),
            ('{"accessToken": "short-token"}', "short-token"),
            ("Bearer auth-secret", "auth-secret"),
            ("https://person:password@crm.test/activate#token=abc", "token=abc"),
            ("postgresql+asyncpg://dbuser:dbpass@db/crm", "dbpass"),
            ("Sent to private@example.com", "private@example.com"),
            ('{"body": "confidential email message"}', "confidential email message"),
            ('parameters: ("private", "values")', "private"),
            ("token=abcdefghijklmnopqrstuvwx12345678", "abcdefghijklmnopqrstuvwx12345678"),
        ]
        for message, secret in cases:
            with self.subTest(message=message):
                self.assertNotIn(secret, clean.clean(message))

    def test_sql_and_parameters_are_omitted(self):
        self.assertEqual(
            LogRedactor().clean("SELECT email FROM users WHERE email='private'"), "Database statement omitted"
        )

    def test_exception_details_keep_frames_without_exception_message_or_locals(self):
        try:
            raise ValueError("password=private-exception-value")
        except ValueError:
            details = LogRedactor().exception_details(sys.exc_info())
        self.assertIn("ValueError", details)
        self.assertIn("test_runtime_logging.py:", details)
        self.assertNotIn("private-exception-value", details)
        self.assertNotIn("raise ValueError", details)


class HandlerTests(unittest.TestCase):
    def test_module_severity_and_source_are_captured(self):
        handler = RuntimeLogHandler("worker", LogRedactor())
        record = logging.LogRecord(
            "crm.invitations", logging.ERROR, __file__, 1, "Delivery failed: %s", ("password=secret",), None
        )
        handler.emit(record)
        data = handler.records.get_nowait()
        self.assertEqual(data["module"], "crm.invitations")
        self.assertEqual(data["source"], "worker")
        self.assertEqual(data["level"], "ERROR")
        self.assertNotIn("secret", data["message"])

    def test_database_log_cannot_expose_parameters(self):
        handler = RuntimeLogHandler("api", LogRedactor())
        handler.emit(
            logging.LogRecord(
                "sqlalchemy.engine", logging.INFO, __file__, 1, "arbitrary private db content", (), None
            )
        )
        self.assertEqual(handler.records.get_nowait()["message"], "Database diagnostic event")

    def test_storage_and_duplicate_request_logs_do_not_recurse(self):
        handler = RuntimeLogHandler("api", LogRedactor())
        for name in ("crm.logs", "crm.requests", "uvicorn.access"):
            handler.emit(logging.LogRecord(name, logging.ERROR, __file__, 1, "message", (), None))
        token = LOG_WRITE_IN_PROGRESS.set(True)
        try:
            handler.emit(
                logging.LogRecord("sqlalchemy.pool", logging.ERROR, __file__, 1, "message", (), None)
            )
        finally:
            LOG_WRITE_IN_PROGRESS.reset(token)
        self.assertTrue(handler.records.empty())

    def test_capacity_is_bounded_and_drops_are_counted(self):
        handler = RuntimeLogHandler("api", LogRedactor(), capacity=1)
        for _ in range(2):
            handler.emit(logging.LogRecord("crm.test", logging.INFO, __file__, 1, "message", (), None))
        self.assertEqual(handler.records.qsize(), 1)
        self.assertEqual(handler.dropped, 1)

    def test_duplicate_logger_propagation_is_captured_once(self):
        handler = RuntimeLogHandler("api", LogRedactor())
        record = logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1, "started", (), None)
        handler.emit(record)
        handler.emit(record)
        self.assertEqual(handler.records.qsize(), 1)


class CaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_flushes_and_restores_handlers_without_database_access(self):
        settings = SimpleNamespace(
            secret_key="private-config-key",
            smtp_password="smtp-password",
            brevo_api_key="brevo-key",
            database_url="postgresql+asyncpg://user:dbpassword@db/crm",
            default_platform_admin_password=SecretStr("admin-password"),
            backend_log_level="INFO",
        )
        root = logging.getLogger()
        before_handlers = list(root.handlers)
        before_level = root.level
        with (
            patch.object(runtime_logging, "get_settings", return_value=settings),
            patch.object(runtime_logging, "record_backend_log", new_callable=AsyncMock) as save,
        ):
            async with RuntimeLogCapture("api"):
                logging.getLogger("crm.capture.test").warning("Test event password=sensitive")
                # Exiting the context drains the bounded queue.
            self.assertGreaterEqual(save.await_count, 1)
            record = next(
                call.kwargs for call in save.call_args_list if call.kwargs.get("module") == "crm.capture.test"
            )
            self.assertNotIn("sensitive", record["message"])
        self.assertEqual(root.handlers, before_handlers)
        self.assertEqual(root.level, before_level)
