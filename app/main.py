from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api_keys.router import router as api_keys_router
from app.auth.errors import APIError
from app.auth.router import router as auth_router
from app.dashboard.router import router as dashboard_router
from app.monitors.router import router as monitors_router
from app.organizations.router import invitations_router
from app.organizations.router import router as organizations_router
from app.projects.router import router as projects_router
from app.routes import health
from app.util.database import dispose_engine
from app.util.errors import register_error_handlers
from app.util.logs import register_logging
from app.util.metrics import register_metrics
from app.util.redis import close_redis_client
from app.websocket.router import router as ws_router


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    try:
        yield
    finally:
        await dispose_engine()
        await close_redis_client()


app = FastAPI(title="UpTimeTracker API", version="0.1.0", lifespan=lifespan)
register_logging(app)
register_metrics(app)
register_error_handlers(app)
app.include_router(auth_router)
app.include_router(organizations_router)
app.include_router(invitations_router)
app.include_router(projects_router)
app.include_router(monitors_router)
app.include_router(api_keys_router)
app.include_router(dashboard_router)
app.include_router(ws_router)
app.include_router(health.router)


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
