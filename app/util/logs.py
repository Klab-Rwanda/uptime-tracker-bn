import json
import logging
import os
import re
import sys
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware

# Holds the current request's ID. Later you can add organization_id the same way.
request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)

# Attributes every LogRecord has. Anything else was passed with extra={...}.
_STANDARD_ATTRS = set(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
) | {"message", "asctime", "taskName"}

# Fields that must never appear in logs, even by mistake
_SENSITIVE = {"password", "token", "access_token", "refresh_token", "secret", "api_key", "authorization"}

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = request_id_ctx.get()
        if request_id:
            entry["request_id"] = request_id

        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS or key.startswith("_"):
                continue
            entry[key] = "[REDACTED]" if key.lower() in _SENSITIVE else value

        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(entry, default=str)


def setup_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    # Send uvicorn's logs through our JSON handler, and skip its plain-text
    # access log because our middleware logs every request itself.
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
    access = logging.getLogger("uvicorn.access")
    access.handlers = []
    access.propagate = False


class RequestLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        incoming = request.headers.get("x-request-id", "")
        request_id = incoming if _REQUEST_ID_RE.match(incoming) else uuid.uuid4().hex
        request_id_ctx.set(request_id)

        logger = logging.getLogger("app.request")
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration = round((time.perf_counter() - start) * 1000, 1)
            logger.error(
                "request failed",
                extra={"method": request.method, "path": request.url.path,
                       "status": 500, "duration_ms": duration},
            )
            raise

        duration = round((time.perf_counter() - start) * 1000, 1)
        logger.info(
            "request completed",
            extra={"method": request.method, "path": request.url.path,
                   "status": response.status_code, "duration_ms": duration},
        )
        response.headers["X-Request-ID"] = request_id
        return response


def register_logging(app: FastAPI) -> None:
    setup_logging()
    app.add_middleware(RequestLogMiddleware)
