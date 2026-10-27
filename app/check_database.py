"""Read-only startup connection check, with errors that never print credentials."""

import asyncio
import socket
import sys

import asyncpg
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings


def connection_error_message(error, host):
    # SQLAlchemy may wrap a driver's original exception.
    error = getattr(error, "orig", error)
    if isinstance(error, socket.gaierror):
        hint = (
            (
                "Docker service names only resolve inside their Docker network. "
                "For run_backend.py, use the database's published host address and port."
            )
            if host in {"postgres", "db", "database"}
            else "Check the database hostname, DNS, and network connection."
        )
        return f"Cannot resolve database host '{host}'. {hint}"
    if isinstance(error, (asyncpg.InvalidPasswordError, asyncpg.InvalidAuthorizationSpecificationError)):
        return "PostgreSQL rejected authentication. Check the user, password, and intended server in DATABASE_URL (.env)."
    if isinstance(error, asyncpg.InvalidCatalogNameError):
        return "The configured PostgreSQL database does not exist. Create it or correct the database name in DATABASE_URL (.env)."
    if isinstance(error, (TimeoutError, ConnectionError, OSError)):
        return "Cannot reach PostgreSQL. Check that it is running and that DATABASE_URL uses the correct reachable host and port."
    return "Database connection check failed. Check DATABASE_URL, PostgreSQL permissions, and TLS settings."


async def check_database():
    try:
        settings = get_settings()
    except ValidationError as error:
        print("Backend configuration is invalid:", file=sys.stderr)
        for issue in error.errors():
            # Avoid Pydantic's default representation, which may include secret input values.
            location = ".".join(str(part) for part in issue["loc"]) or "configuration"
            print(f"  {location}: {issue['msg']}", file=sys.stderr)
        return 1
    engine = None
    host = "configured server"
    try:
        host = make_url(settings.database_url).host or "localhost"
        engine = create_async_engine(settings.database_url, poolclass=NullPool, connect_args={"timeout": 5})
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as error:
        print(connection_error_message(error, host), file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            await engine.dispose()
    print("PostgreSQL connection verified.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(check_database()))
