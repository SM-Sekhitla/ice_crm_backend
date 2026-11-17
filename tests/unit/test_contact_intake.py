"""Public QR intake, ownership and abuse checks without live services."""
import os
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError

os.environ.setdefault("SECRET_KEY", "test-only-secret-key-with-more-than-32-characters")
from app.api import contact_intake as api
from app.core.security import now, token_hash
from app.db.session import get_session
from app.middleware.security import SecurityMiddleware

TOKEN = "A" * 64
OWNER = SimpleNamespace(id="user", active=True, password_set=True, deleted_at=None)

class ContactIntakeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session = MagicMock()
        self.session.get = AsyncMock(return_value=None)
        self.session.scalar = AsyncMock(return_value=None)
        self.session.flush = AsyncMock()
        self.session.execute = AsyncMock(return_value=MagicMock())
        self.settings = SimpleNamespace(secret_key="unit-test-secret-32-characters-long", frontend_base_url="https://crm.example.com")

    def link(self, **overrides):
        return SimpleNamespace(**(dict(user_id="user", enabled=True, digest=token_hash(TOKEN), encrypted_token=api.cipher().encrypt(TOKEN.encode()).decode(), window_started_at=now(), submission_count=0) | overrides))

    def details(self, **overrides):
        return api.IntakeInput(**(dict(token=TOKEN, firstName="  Alex  ", lastName="Smith", email="Alex@Example.com", phone="+27 71 234 5678", company="Example", jobTitle="Director") | overrides))

    def test_fields_are_trimmed_and_extra_ownership_consent_fields_rejected(self):
        self.assertEqual(self.details().first_name, "Alex")
        for fields in [dict(firstName=" "), dict(email="not-an-email"), dict(token="bad"), dict(company="x" * 201), dict(capturedBy="someone"), dict(consent=True), dict(productIds=["secret"]), dict(source="admin")]:
            with self.assertRaises(ValidationError):
                self.details(**fields)

    async def test_link_creation_is_own_account_only_and_stores_encrypted_token(self):
        with patch.object(api, "get_settings", return_value=self.settings):
            result = await api.create_link(api.LinkInput(), self.session, OWNER)
            link = next(call.args[0] for call in self.session.add.call_args_list if hasattr(call.args[0], "digest"))
            token = result["url"].split("#token=")[1]
            self.assertEqual(len(token), 64)
            self.assertEqual(link.user_id, OWNER.id)
            self.assertEqual(link.digest, token_hash(token))
            self.assertNotIn(token, link.encrypted_token)
            self.assertEqual(api.cipher().decrypt(link.encrypted_token.encode()).decode(), token)
            self.assertTrue(result["url"].startswith("https://crm.example.com/connect#token="))

    async def test_existing_link_is_stable_and_replacement_invalidates_old_digest(self):
        with patch.object(api, "get_settings", return_value=self.settings):
            link = self.link()
            self.session.get.return_value = link
            result = await api.create_link(api.LinkInput(), self.session, OWNER)
            self.assertTrue(result["url"].endswith(TOKEN))
            self.session.flush.assert_not_awaited()
            await api.create_link(api.LinkInput(replace=True), self.session, OWNER)
            self.assertNotEqual(link.digest, token_hash(TOKEN))

    async def test_disable_and_invalid_owner_make_form_unavailable(self):
        with patch.object(api, "get_settings", return_value=self.settings):
            link = self.link()
            self.session.get.return_value = link
            await api.disable_link(self.session, OWNER)
            self.assertFalse(link.enabled)
            self.assertEqual(api.link_response(link), {"enabled": False, "url": None})
            self.session.scalar.return_value = None
            with self.assertRaises(HTTPException) as error:
                await api.available_link(self.session, TOKEN)
            self.assertEqual(error.exception.status_code, 404)
            self.session.scalar.return_value = self.link()
            for bad_owner in [None, SimpleNamespace(active=False), SimpleNamespace(active=True, password_set=False), SimpleNamespace(active=True, password_set=True, deleted_at=now())]:
                self.session.get.return_value = bad_owner
                with self.assertRaises(HTTPException):
                    await api.available_link(self.session, TOKEN)

    async def test_submit_has_fixed_capture_attribution_no_marketing_consent_and_audit(self):
        self.session.execute.return_value.scalar_one_or_none.return_value = "new-contact"
        with patch.object(api, "available_link", AsyncMock(return_value=(self.link(), OWNER))):
            result = await api.submit_contact(self.details(), self.session)
        statement = self.session.execute.call_args.args[0]
        from sqlalchemy.dialects.postgresql import dialect
        compiled = statement.compile(dialect=dialect())
        self.assertIn("ON CONFLICT (email) DO NOTHING", str(compiled))
        self.assertEqual(compiled.params["email"], "alex@example.com")
        self.assertEqual(compiled.params["captured_by"], "user")
        self.assertIs(compiled.params["consent"], False)
        self.assertNotIn("token", compiled.params)
        audit_record = self.session.add.call_args.args[0]
        self.assertEqual(audit_record.contact_id, "new-contact")
        self.assertEqual(audit_record.user_id, "user")
        self.assertEqual(result, api.RECEIVED)
        self.assertNotIn("id", result)

    async def test_duplicate_is_same_success_without_updates_or_extra_audit(self):
        self.session.execute.return_value.scalar_one_or_none.return_value = None
        with patch.object(api, "available_link", AsyncMock(return_value=(self.link(), OWNER))):
            result = await api.submit_contact(self.details(), self.session)
        self.assertEqual(result, api.RECEIVED)
        self.session.add.assert_not_called()
        self.assertNotIn("DO UPDATE", str(self.session.execute.call_args.args[0]))

    async def test_database_link_rate_limit_and_window_reset(self):
        link = self.link(submission_count=300)
        with patch.object(api, "available_link", AsyncMock(return_value=(link, OWNER))):
            with self.assertRaises(HTTPException) as error:
                await api.submit_contact(self.details(), self.session)
            self.assertEqual(error.exception.status_code, 429)
            self.session.execute.assert_not_awaited()
            link.window_started_at = now() - timedelta(hours=2)
            self.session.execute.return_value.scalar_one_or_none.return_value = None
            await api.submit_contact(self.details(), self.session)
            self.assertEqual(link.submission_count, 1)

    async def test_http_public_verification_exposes_no_private_fields_and_management_needs_auth(self):
        app = FastAPI()
        app.include_router(api.router, prefix="/api/v1")
        app.dependency_overrides[get_session] = lambda: self.session
        with patch.object(api, "available_link", AsyncMock(return_value=(self.link(), OWNER))):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                response = await client.post("/api/v1/public/contact-intake/verify", json={"token": TOKEN})
                self.assertEqual(response.json(), {"available": True})
                self.assertEqual((await client.get("/api/v1/contact-intake-link")).status_code, 401)
                self.assertEqual((await client.post("/api/v1/contact-intake-link", json={})).status_code, 401)
                self.assertEqual((await client.delete("/api/v1/contact-intake-link")).status_code, 401)
                invalid = await client.post("/api/v1/public/contact-intake/submit", json={"token": TOKEN, "email": "bad"})
                self.assertEqual(invalid.status_code, 422)

    async def test_public_http_throttle_works_without_auth_or_proxy_header_trust(self):
        app = FastAPI()
        app.include_router(api.router, prefix="/api/v1")
        app.dependency_overrides[get_session] = lambda: self.session
        app.add_middleware(SecurityMiddleware)
        settings = SimpleNamespace(request_max_bytes=2000000, auth_rate_limit=20, environment="development")
        with patch.object(api, "available_link", AsyncMock(return_value=(self.link(), OWNER))), patch("app.middleware.security.get_settings", return_value=settings), patch("app.middleware.security.record_backend_log", AsyncMock()):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                for index in range(60):
                    response = await client.post("/api/v1/public/contact-intake/verify", json={"token": TOKEN}, headers={"x-forwarded-for": str(index)})
                    self.assertEqual(response.status_code, 200)
                response = await client.post("/api/v1/public/contact-intake/verify", json={"token": TOKEN})
                self.assertEqual(response.status_code, 429)
                self.assertEqual(response.headers["retry-after"], "60")
