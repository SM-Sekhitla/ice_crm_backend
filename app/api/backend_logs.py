"""Virtual log files backed by retained structured records, not arbitrary disk paths."""

from datetime import timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select

from app.api.deps import Session, require
from app.core.security import now
from app.db.models import BackendLog
from app.services.backend_logs import RETENTION_DAYS
from app.services.common import serialize

router = APIRouter(tags=["Administration"])
Level = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
Service = Literal["api", "email-worker"]
Kind = Literal["requests", "system", "delivery"]
MODULE_PATTERN = r"^[A-Za-z0-9_.-]*$"


def filtered_logs(
    *, level=None, source=None, service=None, module="", module_exact="", kind=None, q="", before=None
):
    query = select(BackendLog).where(BackendLog.created_at >= now() - timedelta(days=RETENTION_DAYS))
    if level:
        query = query.where(BackendLog.level == level)
    if source:
        query = query.where(BackendLog.source == source)
    if service:
        query = query.where(BackendLog.service == service)
    if module_exact:
        query = query.where(func.coalesce(BackendLog.module, "application") == module_exact)
    elif module:
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
    return query


def log_text(row):
    stamp = row.created_at.astimezone(timezone.utc).isoformat(timespec="milliseconds")
    message = row.message.replace("\r", "").replace("\n", "\n    ")
    text = f"{stamp} {row.level:<8} {row.service or row.source}/{row.module or 'application'} {message}"
    for key, value in (
        ("method", row.method),
        ("route", row.route),
        ("status", row.status_code),
        ("duration_ms", row.duration_ms),
        ("request", row.request_id),
        ("error", row.error_type),
        ("outcome", row.outcome),
    ):
        if value is not None:
            text += f" {key}={value}"
    if row.details:
        text += "\n    " + row.details.replace("\r", "").replace("\n", "\n    ")
    return text + "\n"


@router.get("/admin/backend-logs/files")
async def log_files(session: Session, user=Depends(require("logs.view"))):
    folders = {}
    for folder, column in (("modules", BackendLog.module), ("services", BackendLog.service)):
        group_key = func.coalesce(column, "application" if folder == "modules" else "api")
        rows = await session.execute(
            select(
                group_key,
                func.count(BackendLog.id),
                func.max(BackendLog.created_at),
            )
            .where(BackendLog.created_at >= now() - timedelta(days=RETENTION_DAYS))
            .group_by(group_key)
            .order_by(group_key)
        )
        folders[folder] = [
            {"key": key, "name": f"{key}.log", "entries": count, "modifiedAt": modified}
            for key, count, modified in rows
        ]
    return {"folders": folders, "retentionDays": RETENTION_DAYS}


@router.get("/admin/backend-logs/export")
async def export_logs(
    session: Session,
    user=Depends(require("logs.view")),
    level: Level | None = None,
    service: Service | None = None,
    module_exact: str = Query(default="", max_length=120, pattern=MODULE_PATTERN),
    kind: Kind | None = None,
    q: str = Query(default="", max_length=120),
):
    rows = list(
        await session.scalars(
            filtered_logs(level=level, service=service, module_exact=module_exact, kind=kind, q=q)
            .order_by(BackendLog.id.desc())
            .limit(2001)
        )
    )
    return {
        "filename": f"{module_exact or service or 'system'}.log",
        "content": "".join(log_text(row) for row in reversed(rows[:2000])),
        "truncated": len(rows) > 2000,
        "count": min(len(rows), 2000),
    }


@router.get("/admin/backend-logs")
async def backend_logs(
    session: Session,
    user=Depends(require("logs.view")),
    level: Level | None = None,
    source: Literal["api", "worker"] | None = None,
    service: Service | None = None,
    module: str = Query(default="", max_length=120, pattern=MODULE_PATTERN),
    module_exact: str = Query(default="", max_length=120, pattern=MODULE_PATTERN),
    kind: Kind | None = None,
    q: str = Query(default="", max_length=120),
    before: int | None = Query(default=None, gt=0),
    limit: int = Query(default=50, ge=1, le=100),
):
    query = filtered_logs(
        level=level,
        source=source,
        service=service,
        module=module,
        module_exact=module_exact,
        kind=kind,
        q=q,
        before=before,
    )
    rows = list(await session.scalars(query.order_by(BackendLog.id.desc()).limit(limit + 1)))
    more = len(rows) > limit
    rows = rows[:limit]
    return {
        "items": [{**serialize(row), "text": log_text(row)} for row in rows],
        "hasMore": more,
        "nextCursor": rows[-1].id if more else None,
        "retentionDays": RETENTION_DAYS,
        "coverage": "API and email worker runtime logs; host, build and container platform logs are not streamed.",
    }
