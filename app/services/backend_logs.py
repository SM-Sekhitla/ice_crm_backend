"""Bounded operational and sanitized runtime events shared across processes."""

import asyncio
import logging
import re
import time
from contextvars import ContextVar
from datetime import timedelta

from sqlalchemy import delete

from app.core.security import now
from app.db.models import BackendLog
from app.db.session import SessionLocal

RETENTION_DAYS = 7
EVENTS = {
    "http_request": "HTTP request completed",
    "api_started": "API started",
    "api_stopped": "API stopping",
    "worker_stopped": "Email worker stopping",
    "system_log": "Runtime log event",
    "worker_started": "Email worker started",
    "worker_error": "Email queue processing failed",
    "invitation_delivery": "Invitation delivery processed",
    "campaign_delivery": "Campaign recipient processed",
}
LOG_WRITE_IN_PROGRESS = ContextVar("crm_log_write_in_progress", default=False)
_last_cleanup = 0.0
_last_warning = 0.0


async def record_backend_log(
    event,
    *,
    source="api",
    level="INFO",
    request_id=None,
    method=None,
    route=None,
    status_code=None,
    duration_ms=None,
    error_type=None,
    outcome=None,
    module=None,
    message=None,
    details=None,
    created_at=None,
):
    """Logging failures must not fail an API request or cause email retries."""
    global _last_cleanup, _last_warning
    if (
        event not in EVENTS
        or source not in {"api", "worker"}
        or level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    ):
        return
    # Routes come from FastAPI's registered template, never raw URLs/query strings.
    route = route[:200] if route and re.fullmatch(r"[/a-zA-Z0-9_{}.-]+", route) else None
    request_id = request_id if request_id and re.fullmatch(r"[a-f0-9-]{36}", request_id) else None
    method = method if method in {"GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"} else None
    error_type = (
        error_type[:100] if error_type and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", error_type) else None
    )
    outcome = (
        outcome
        if outcome in {"Sent", "Accepted", "Failed", "Unknown", "Simulated", "Skipped", "Cancelled"}
        else None
    )
    default_module = {
        "http_request": "app.middleware.security",
        "invitation_delivery": "app.services.invitations",
        "api_started": "app.main",
        "api_stopped": "app.main",
    }.get(event, "app.services.worker")
    module = module if module and re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", module) else default_module
    service = "email-worker" if source == "worker" else "api"
    write_token = LOG_WRITE_IN_PROGRESS.set(True)
    try:
        async with asyncio.timeout(0.5), SessionLocal() as session, session.begin():
            session.add(
                BackendLog(
                    source=source,
                    level=level,
                    event=event,
                    message=str(message)[:2000] if event == "system_log" and message else EVENTS[event],
                    module=module,
                    service=service,
                    details=str(details)[:8000] if event == "system_log" and details else None,
                    **({"created_at": created_at} if created_at is not None else {}),
                    request_id=request_id,
                    method=method,
                    route=route,
                    status_code=status_code,
                    duration_ms=round(duration_ms, 1) if duration_ms is not None else None,
                    error_type=error_type,
                    outcome=outcome,
                )
            )
            timestamp = time.monotonic()
            if timestamp - _last_cleanup > 3600:
                await session.execute(
                    delete(BackendLog).where(BackendLog.created_at < now() - timedelta(days=RETENTION_DAYS))
                )
                _last_cleanup = timestamp
    except Exception:
        timestamp = time.monotonic()
        if timestamp - _last_warning > 60:
            logging.getLogger("crm.logs").warning("Operational log storage unavailable; event omitted")
            _last_warning = timestamp

    finally:
        LOG_WRITE_IN_PROGRESS.reset(write_token)
