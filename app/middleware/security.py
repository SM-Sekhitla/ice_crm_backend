"""PRISM-inspired security headers, request correlation and login throttling.

Bearer-only auth deliberately avoids ambient cookies and cookie-CSRF exposure.
"""

import json
import logging
import time
from collections import defaultdict, deque
from uuid import uuid4

from app.core.config import get_settings

logger = logging.getLogger("crm.requests")


class SecurityMiddleware:
    def __init__(self, app):
        self.app = app
        self.attempts = defaultdict(deque)
        self.last_cleanup = 0.0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        settings = get_settings()
        started = time.monotonic()
        request_id = str(uuid4())  # Never log untrusted supplied request IDs.
        headers = dict(scope.get("headers", []))
        length = headers.get(b"content-length", b"0")
        try:
            oversized = int(length) > settings.request_max_bytes or int(length) < 0
        except ValueError:
            oversized = True

        async def reject(code, message, extra=None):
            await send(
                {
                    "type": "http.response.start",
                    "status": code,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"x-request-id", request_id.encode()),
                        *(extra or []),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": json.dumps({"detail": message}).encode()})

        if oversized:
            return await reject(413, "Request body too large")
        if scope["path"].startswith("/api/v1/auth/") and scope["method"] == "POST":
            key = (scope.get("client") or ("unknown",))[0]  # No trust in forwarded IPs.
            timestamp = time.monotonic()
            if timestamp - self.last_cleanup > 60:
                self.attempts = defaultdict(
                    deque, {k: v for k, v in self.attempts.items() if v and v[-1] > timestamp - 60}
                )
                self.last_cleanup = timestamp
            attempts = self.attempts[key]
            while attempts and attempts[0] <= timestamp - 60:
                attempts.popleft()
            if len(attempts) >= settings.auth_rate_limit:
                return await reject(429, "Too many authentication attempts", [(b"retry-after", b"60")])
            attempts.append(timestamp)
        # Buffer up to the limit before routing; chunked requests cannot bypass limits.
        chunks = []
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > settings.request_max_bytes:
                return await reject(413, "Request body too large")
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        consumed = False

        async def replay():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        async def wrapped_send(message):
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + [
                    (b"x-request-id", request_id.encode()),
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"cache-control", b"no-store"),
                ]
                if settings.environment == "production":
                    message["headers"].append(
                        (b"strict-transport-security", b"max-age=31536000; includeSubDomains")
                    )
            await send(message)

        try:
            await self.app(scope, replay, wrapped_send)
        finally:
            logger.info(
                "request=%s method=%s duration_ms=%.1f",
                request_id,
                scope["method"],
                (time.monotonic() - started) * 1000,
            )
