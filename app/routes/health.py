import asyncio
import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.util.database import check_database
from app.util.redis import check_redis

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

# Neon can take a few seconds to wake from idle, so don't set this too low
CHECK_TIMEOUT_SECONDS = 15.0


async def _run_check(name: str, check) -> str:
    try:
        await asyncio.wait_for(check(), timeout=CHECK_TIMEOUT_SECONDS)
        return "ok"
    except Exception:
        # Details go to the logs only, never to the client
        logger.warning("readiness check failed", extra={"check": name}, exc_info=True)
        return "error"


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def ready() -> JSONResponse:
    database, redis = await asyncio.gather(
        _run_check("database", check_database),
        _run_check("redis", check_redis),
    )
    checks = {"database": database, "redis": redis}
    healthy = all(v == "ok" for v in checks.values())
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "error", "checks": checks},
    )
