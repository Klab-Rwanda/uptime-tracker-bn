from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api_keys.router import router as api_keys_router
from app.audit.router import router as audit_router
from app.auth.errors import APIError
from app.auth.router import router as auth_router
from app.dashboard.router import router as dashboard_router
from app.heartbeats.router import router as heartbeats_router
from app.incidents.router import router as incidents_router
from app.maintenance.router import router as maintenance_router
from app.monitors.router import router as monitors_router
from app.notification_channels.router import router as notification_channels_router
from app.organizations.router import invitations_router
from app.organizations.router import router as organizations_router
from app.projects.router import router as projects_router
from app.routes import health
from app.routes.checks import router as checks_router
from app.status_pages.router import public_router as public_status_router
from app.status_pages.router import router as status_pages_router
from app.util.cors import register_cors
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


app = FastAPI(title="PulseMonitor API", version="0.1.0", lifespan=lifespan)
register_logging(app)
register_metrics(app)
register_error_handlers(app)
# Last, so it wraps the other middleware: 401/404/422 answers carry the CORS headers too
register_cors(app)
app.include_router(auth_router)
app.include_router(organizations_router)
app.include_router(invitations_router)
app.include_router(projects_router)
app.include_router(monitors_router)
app.include_router(heartbeats_router)
app.include_router(incidents_router)
app.include_router(maintenance_router)
app.include_router(status_pages_router)
app.include_router(public_status_router)
app.include_router(notification_channels_router)
app.include_router(api_keys_router)
app.include_router(audit_router)
app.include_router(dashboard_router)
app.include_router(ws_router)
app.include_router(health.router)
app.include_router(checks_router)


# Restored: a merge dropped this handler, which turned every APIError (401, 403,
# 404, 409, 422 ...) into a 500. tests/test_app_errors.py now guards it.
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
