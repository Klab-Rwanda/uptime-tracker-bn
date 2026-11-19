from fastapi import FastAPI

from app.util.errors import register_error_handlers

app = FastAPI(title="UpTimeTracker API", version="0.1.0")
register_error_handlers(app)


@app.get("/health/live")
async def health_live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
async def health_ready() -> dict[str, str]:
    return {"status": "ok"}
