from fastapi import FastAPI

from app.util.errors import register_error_handlers
from app.util.logs import register_logging

app = FastAPI(title="UpTimeTracker API", version="0.1.0")
register_logging(app)
register_error_handlers(app)


@app.get("/health/live")
async def health_live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
async def health_ready() -> dict[str, str]:
    return {"status": "ok"}
