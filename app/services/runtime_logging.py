"""Bounded logging capture for API/worker processes, including framework events."""

import asyncio
import logging
import queue
import re
import traceback
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from app.core.config import get_settings
from app.services.backend_logs import LOG_WRITE_IN_PROGRESS, record_backend_log


class LogRedactor:
    def __init__(self, secrets=()):
        self.secrets = sorted(
            {str(value) for value in secrets if value and len(str(value)) >= 4}, key=len, reverse=True
        )

    def clean(self, value, limit=2000):
        text = str(value)[:16000]
        for secret in self.secrets:
            text = text.replace(secret, "[REDACTED]")
        if re.search(r"(?i)<(?:html|body)\b", text):
            return "HTML content omitted"
        text = re.sub(
            r"(?is)\b(?:body|payload|headers|parameters|params)[\"\']?\s*[:=].*", "[content omitted]", text
        )
        if re.search(r"(?i)\b(?:select\b.+\bfrom|insert\s+into|update\b.+\bset|delete\s+from)\b", text):
            return "Database statement omitted"
        text = re.sub(
            r"(?i)https?://[^\s<>\"']+|postgres(?:ql)?(?:\+asyncpg)?://[^\s<>\"']+", "[URL omitted]", text
        )
        text = re.sub(r"(?i)\bbearer\s+[^\s,;]+", "Bearer [REDACTED]", text)
        text = re.sub(
            r"""(?ix)\b(?:[A-Za-z0-9_.-]*[_-])?(?:password|passwd|pwd|secret(?:_key)?|(?:access[_-]?|refresh[_-]?)?token|api[-_ ]?key|authorization|cookie)\b["']?\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,;]+)""",
            "[credential redacted]",
            text,
        )
        text = re.sub(
            r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[email omitted]", text
        )
        text = re.sub(r"\b[A-Za-z0-9_~+/=-]{24,}\b", "[opaque value omitted]", text)
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
        return text[:limit]

    def exception_details(self, exc_info):
        if not exc_info or not exc_info[1]:
            return None
        parts = []
        error = exc_info[1]
        seen = set()
        while error and id(error) not in seen and len(seen) < 5:
            seen.add(id(error))
            parts.append(type(error).__name__)
            for frame in traceback.extract_tb(error.__traceback__)[-30:]:
                parts.append(f"  {Path(frame.filename).name}:{frame.lineno} in {frame.name}")
            error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
            if error:
                parts.append("Caused by:")
        # No exception strings, SQL parameters, source lines, locals or request bodies.
        return self.clean("\n".join(parts), 8000)


class RuntimeLogHandler(logging.Handler):
    def __init__(self, source, redactor, capacity=2000, level=logging.INFO):
        super().__init__(level=level)
        self.source = source
        self.redactor = redactor
        self.records = queue.Queue(maxsize=capacity)
        self.dropped = 0

    def emit(self, record):
        # Structured requests already exist; never recurse through storage warnings.
        if LOG_WRITE_IN_PROGRESS.get() or record.name.startswith(
            ("crm.logs", "crm.requests", "uvicorn.access")
        ):
            return
        if getattr(record, "_crm_runtime_capture", None) == id(self):
            return
        record._crm_runtime_capture = id(self)
        try:
            module = record.name if re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", record.name) else "application"
            # Driver diagnostics can contain SQL/parameters even at INFO level.
            message = (
                "Database diagnostic event"
                if record.name.startswith(("sqlalchemy", "asyncpg"))
                else self.redactor.clean(record.getMessage())
            )
            details = self.redactor.exception_details(record.exc_info)
            level = (
                "CRITICAL"
                if record.levelno >= 50
                else "ERROR"
                if record.levelno >= 40
                else "WARNING"
                if record.levelno >= 30
                else "INFO"
                if record.levelno >= 20
                else "DEBUG"
            )
            self.records.put_nowait(
                {
                    "source": self.source,
                    "level": level,
                    "module": module,
                    "message": message,
                    "details": details,
                    "error_type": type(record.exc_info[1]).__name__
                    if record.exc_info and record.exc_info[1]
                    else None,
                    "created_at": datetime.fromtimestamp(record.created, UTC),
                }
            )
        except queue.Full:
            self.dropped += 1
        except Exception:
            # A malformed LogRecord must not interrupt the application.
            self.dropped += 1


class RuntimeLogCapture:
    def __init__(self, source):
        self.source = source
        self.task = None
        self.stopping = False

    async def __aenter__(self):
        settings = get_settings()
        values = [
            settings.secret_key,
            settings.smtp_password,
            settings.brevo_api_key,
            settings.database_url,
            settings.default_platform_admin_password.get_secret_value(),
        ]
        password = urlsplit(settings.database_url).password
        if password:
            from urllib.parse import unquote

            values.extend((password, unquote(password)))
        capture_level = getattr(logging, settings.backend_log_level)
        self.handler = RuntimeLogHandler(self.source, LogRedactor(values), level=capture_level)
        self.loggers = [logging.getLogger(), logging.getLogger("uvicorn")]
        self.old_levels = [logger.level for logger in self.loggers]
        for logger in self.loggers:
            logger.addHandler(self.handler)
            if logger.getEffectiveLevel() > capture_level:
                logger.setLevel(capture_level)
        self.task = asyncio.create_task(self.consume(), name="crm-runtime-log-capture")
        return self

    async def consume(self):
        while not self.stopping or not self.handler.records.empty():
            batch = []
            for _ in range(10):
                try:
                    batch.append(self.handler.records.get_nowait())
                except queue.Empty:
                    break
            if batch:
                await asyncio.gather(*(record_backend_log("system_log", **record) for record in batch))
            else:
                await asyncio.sleep(0.2)
            self.handler.acquire()
            try:
                dropped, self.handler.dropped = self.handler.dropped, 0
            finally:
                self.handler.release()
            if dropped:
                await record_backend_log(
                    "system_log",
                    source=self.source,
                    level="WARNING",
                    module="logging",
                    message=f"Log buffer capacity exceeded; {dropped} events omitted",
                )

    async def __aexit__(self, *args):
        for logger, level in zip(self.loggers, self.old_levels, strict=True):
            logger.removeHandler(self.handler)
            logger.setLevel(level)
        self.stopping = True
        try:
            await asyncio.wait_for(self.task, timeout=3)
        except TimeoutError:
            pass
        finally:
            self.handler.close()
