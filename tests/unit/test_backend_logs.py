"""No database, providers, processes or application services are started."""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import FastAPI

os.environ.setdefault("SECRET_KEY", "test-only-secret-key-with-more-than-32-characters")

from app.api.backend_logs import router
from app.api.deps import current_user
from app.core.security import now
from app.db.models import BackendLog
from app.db.session import get_session
from app.middleware.security import SecurityMiddleware
from app.services import backend_logs


class Context:
    def __init__(self, value=None):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *args):
        return False


class LogStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_structured_safe_fields_are_stored(self):
        session = SimpleNamespace(add=Mock(), execute=AsyncMock(), begin=Mock(return_value=Context()))
        with patch.object(backend_logs, "SessionLocal", Mock(return_value=Context(session))):
            await backend_logs.record_backend_log(
                "http_request",
                route="/api/v1/users/{user_id}",
                method="POST",
                status_code=422,
                error_type="ValueError: secret-password",
                outcome="secret-token",
                request_id="untrusted-token",
                duration_ms=12.345,
            )
        row = session.add.call_args.args[0]
        self.assertEqual(row.route, "/api/v1/users/{user_id}")
        self.assertEqual(row.message, "HTTP request completed")
        self.assertIsNone(row.error_type)
        self.assertIsNone(row.outcome)
        self.assertIsNone(row.request_id)
        self.assertEqual(row.duration_ms, 12.3)

    async def test_raw_urls_and_unknown_events_are_not_stored(self):
        session = SimpleNamespace(add=Mock(), execute=AsyncMock(), begin=Mock(return_value=Context()))
        with patch.object(backend_logs, "SessionLocal", Mock(return_value=Context(session))) as factory:
            await backend_logs.record_backend_log("password=secret")
            factory.assert_not_called()
            await backend_logs.record_backend_log("http_request", route="/login?token=secret")
        self.assertIsNone(session.add.call_args.args[0].route)

    async def test_log_database_failure_does_not_escape(self):
        with patch.object(backend_logs, "SessionLocal", side_effect=RuntimeError("private db details")):
            await backend_logs.record_backend_log("api_started")


class LogApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = SimpleNamespace(scalars=AsyncMock(return_value=[]))
        self.app = FastAPI()
        self.app.include_router(router, prefix="/api/v1")

        async def session():
            yield self.session

        self.app.dependency_overrides[get_session] = session
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://testserver"
        )

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_authentication_is_required(self):
        response = await self.client.get("/api/v1/admin/backend-logs")
        self.assertEqual(response.status_code, 401)
        self.session.scalars.assert_not_awaited()

    async def test_bdo_is_rejected_by_backend(self):
        self.app.dependency_overrides[current_user] = lambda: SimpleNamespace(role="BDO")
        response = await self.client.get("/api/v1/admin/backend-logs")
        self.assertEqual(response.status_code, 403)
        self.session.scalars.assert_not_awaited()

    async def test_admin_filters_and_cursor_pagination(self):
        self.app.dependency_overrides[current_user] = lambda: SimpleNamespace(role="Super Admin")
        self.session.scalars.return_value = [
            BackendLog(
                id=i,
                created_at=now(),
                source="api",
                level="ERROR",
                event="http_request",
                message="HTTP request completed",
            )
            for i in (99, 98, 97)
        ]
        response = await self.client.get(
            "/api/v1/admin/backend-logs?level=ERROR&source=api&before=100&limit=2&q=error%25"
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual([row["id"] for row in data["items"]], [99, 98])
        self.assertTrue(data["hasMore"])
        self.assertEqual(data["nextCursor"], 98)
        self.assertEqual(data["retentionDays"], 7)
        query = self.session.scalars.call_args.args[0]
        sql = str(query)
        self.assertIn("backend_logs.id <", sql)
        self.assertIn("backend_logs.level =", sql)
        self.assertIn("backend_logs.source =", sql)
        self.assertNotIn("password", str(data))

    async def test_service_module_category_filters_are_applied(self):
        self.app.dependency_overrides[current_user] = lambda: SimpleNamespace(role="Super Admin")
        response = await self.client.get(
            "/api/v1/admin/backend-logs?service=email-worker&module=invitations&kind=system&level=CRITICAL"
        )
        self.assertEqual(response.status_code, 200)
        sql = str(self.session.scalars.call_args.args[0])
        self.assertIn("backend_logs.service =", sql)
        self.assertIn("backend_logs.module", sql)
        self.assertIn("backend_logs.event IN", sql)
        self.assertIn("coverage", response.json())

    async def test_invalid_filters_and_unbounded_limit_are_rejected(self):
        self.app.dependency_overrides[current_user] = lambda: SimpleNamespace(role="Super Admin")
        for query in (
            "limit=999999",
            "level=SECRET",
            "before=-1",
            "service=host",
            "kind=raw",
            "module=bad%20module",
            "q=" + "a" * 121,
        ):
            response = await self.client.get("/api/v1/admin/backend-logs?" + query)
            self.assertEqual(response.status_code, 422)


class RequestLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_logs_status_route_template_and_request_id_without_sensitive_data(self):
        app = FastAPI()

        @app.post("/example/{secret}")
        async def endpoint(secret: str):
            return {"ok": True}

        app.add_middleware(SecurityMiddleware)
        with patch("app.middleware.security.record_backend_log", new_callable=AsyncMock) as record:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                response = await client.post(
                    "/example/private-value?token=private-token",
                    json={"password": "private-password"},
                    headers={"Authorization": "Bearer private-auth"},
                )
        self.assertEqual(response.status_code, 200)
        fields = record.call_args.kwargs
        self.assertEqual(fields["route"], "/example/{secret}")
        self.assertEqual(fields["status_code"], 200)
        self.assertEqual(fields["request_id"], response.headers["x-request-id"])
        self.assertNotIn("private-", str(record.call_args))

    async def test_polling_logs_does_not_generate_more_log_entries(self):
        app = FastAPI()

        @app.get("/api/v1/admin/backend-logs")
        async def endpoint():
            return {"items": []}

        app.add_middleware(SecurityMiddleware)
        with patch("app.middleware.security.record_backend_log", new_callable=AsyncMock) as record:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                await client.get("/api/v1/admin/backend-logs")
        record.assert_not_awaited()
