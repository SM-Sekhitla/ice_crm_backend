import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api import admin, auth, backend_logs, calendar, campaigns, crm, imports, workspace
from app.core.config import get_settings
from app.db.session import engine
from app.middleware.security import SecurityMiddleware
from app.services.backend_logs import record_backend_log
from app.services.bootstrap import ensure_initial_admin
from app.services.runtime_logging import RuntimeLogCapture


@asynccontextmanager
async def lifespan(app):
    try:
        async with RuntimeLogCapture("api"):
            try:
                await ensure_initial_admin()
                await record_backend_log("api_started")
                yield
            except Exception:
                logging.getLogger("crm.lifecycle").exception("API lifecycle failure")
                raise
            finally:
                await record_backend_log("api_stopped")
    finally:
        await engine.dispose()


def create_app():
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version="1.0.0", lifespan=lifespan)

    @app.exception_handler(IntegrityError)
    async def integrity_error(request, exc):
        # Never expose SQL, parameters, or private conflicting records.
        return JSONResponse(status_code=409, content={"detail": "Duplicate record or protected relationship"})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse(
            status_code=422,
            content={
                "detail": [
                    {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
                    for error in exc.errors()
                ]
            },
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        logging.getLogger("crm.errors").exception("Unhandled API error")
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    @app.get("/health/live", tags=["Health"])
    async def live():
        return {"status": "ok"}

    @app.get("/health/ready", tags=["Health"])
    async def ready():
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1 FROM alembic_version LIMIT 1"))
            return {"status": "ready"}
        except Exception:
            return JSONResponse(status_code=503, content={"status": "unavailable"})

    for router in (
        auth.router,
        calendar.router,
        backend_logs.router,
        admin.router,
        imports.router,
        crm.router,
        campaigns.router,
        workspace.router,
    ):
        app.include_router(router, prefix="/api/v1")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    app.add_middleware(SecurityMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        expose_headers=["X-Request-ID"],
    )
    return app


app = create_app()
