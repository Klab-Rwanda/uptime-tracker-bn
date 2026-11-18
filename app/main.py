from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.auth.errors import APIError
from app.auth.router import router as auth_router
from app.util.redis import close_redis_client


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    yield
    await close_redis_client()

app = FastAPI(title="UpTimeTracker API", version="0.1.0", lifespan=lifespan)
app.include_router(auth_router)


@app.exception_handler(APIError)
async def handle_api_error(_: Request, error: APIError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content={
            "error": {
                "code": error.code,
                "message": error.message,
            }
        },
        headers=error.headers,
    )


@app.exception_handler(RequestValidationError)
async def handle_validation_error(
    _: Request,
    error: RequestValidationError,
) -> JSONResponse:
    details = [
        {
            "field": ".".join(str(part) for part in item["loc"] if part != "body"),
            "message": item["msg"],
        }
        for item in error.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "validation_error",
                "message": "Request validation failed",
                "details": details,
            }
        },
    )


@app.get("/health/live")
async def health_live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
async def health_ready() -> dict[str, str]:
    return {"status": "ok"}
