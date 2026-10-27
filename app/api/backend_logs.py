from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select

from app.api.deps import Session, require
from app.core.security import now
from app.db.models import BackendLog
from app.services.backend_logs import RETENTION_DAYS
from app.services.common import serialize

router = APIRouter(tags=["Administration"])


@router.get("/admin/backend-logs")
async def backend_logs(
    session: Session,
    user=Depends(require("logs.view")),
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] | None = None,
    source: Literal["api", "worker"] | None = None,
    service: Literal["api", "email-worker"] | None = None,
    module: str = Query(default="", max_length=120, pattern=r"^[A-Za-z0-9_.-]*$"),
    kind: Literal["requests", "system", "delivery"] | None = None,
    q: str = Query(default="", max_length=120),
    before: int | None = Query(default=None, gt=0),
    limit: int = Query(default=50, ge=1, le=100),
):
    query = select(BackendLog).where(BackendLog.created_at >= now() - timedelta(days=RETENTION_DAYS))
    if level:
        query = query.where(BackendLog.level == level)
    if source:
        query = query.where(BackendLog.source == source)
    if service:
        query = query.where(BackendLog.service == service)
    if module:
        query = query.where(BackendLog.module.icontains(module, autoescape=True))
    if kind == "requests":
        query = query.where(BackendLog.event == "http_request")
    elif kind == "system":
        query = query.where(
            BackendLog.event.in_(
                [
                    "system_log",
                    "api_started",
                    "api_stopped",
                    "worker_started",
                    "worker_stopped",
                    "worker_error",
                ]
            )
        )
    elif kind == "delivery":
        query = query.where(BackendLog.event.in_(["invitation_delivery", "campaign_delivery"]))
    if before:
        query = query.where(BackendLog.id < before)
    if q.strip():
        query = query.where(
            or_(
                *[
                    field.icontains(q.strip(), autoescape=True)
                    for field in (
                        BackendLog.message,
                        BackendLog.module,
                        BackendLog.service,
                        BackendLog.details,
                        BackendLog.route,
                        BackendLog.request_id,
                        BackendLog.error_type,
                        BackendLog.outcome,
                    )
                ]
            )
        )
    rows = list(await session.scalars(query.order_by(BackendLog.id.desc()).limit(limit + 1)))
    more = len(rows) > limit
    rows = rows[:limit]
    return {
        "items": [serialize(row) for row in rows],
        "hasMore": more,
        "nextCursor": rows[-1].id if more else None,
        "retentionDays": RETENTION_DAYS,
        "coverage": "API and email worker runtime logs; host, build and container platform logs are not streamed.",
    }
