import logging
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

# Error codes for each HTTP status (from the API contract)
ERROR_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "validation_error",
    429: "too_many_requests",
    500: "internal_error",
}


class AppError(Exception):
    """Raise this anywhere in the app to return a standard error.

    Example: raise AppError(409, "conflict", "Slug already taken")
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: list[dict[str, Any]] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        body["details"] = details
    return JSONResponse(status_code=status_code, content={"error": body}, headers=headers)


async def app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    return error_response(exc.status_code, exc.code, exc.message, exc.details)


async def validation_error_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    details = []
    for err in exc.errors():
        loc = [str(part) for part in err["loc"]]
        # Drop the "body" / "query" / "path" prefix so we get just the field name
        if loc and loc[0] in ("body", "query", "path", "header", "cookie"):
            loc = loc[1:]
        details.append({"field": ".".join(loc), "message": err["msg"]})
    return error_response(422, "validation_error", "Request validation failed", details)


async def http_error_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    code = ERROR_CODES.get(exc.status_code, "error")
    message = exc.detail if isinstance(exc.detail, str) else "Request failed"
    return error_response(exc.status_code, code, message, headers=exc.headers)


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # Full details go to the logs only, never to the client
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return error_response(500, "internal_error", "Internal server error")


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)
