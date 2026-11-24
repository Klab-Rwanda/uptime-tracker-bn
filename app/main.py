import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.routes import health
from app.routes.checks import router as checks_router
from app.util.database import dispose_engine
from app.util.errors import register_error_handlers
from app.util.logs import register_logging
from app.util.redis import close_redis
from app.workers.scheduler import run_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = asyncio.create_task(run_scheduler())
    try:
        yield
    finally:
        scheduler.cancel()
        await dispose_engine()
        await close_redis()


app = FastAPI(title="PulseMonitor API", version="0.1.0", lifespan=lifespan)
register_logging(app)
register_error_handlers(app)

app.include_router(health.router)
app.include_router(checks_router)
