# from fastapi import FastAPI

# app = FastAPI(title="UpTimeTracker API", version="0.1.0")


# @app.get("/health/live")
# async def health_live() -> dict[str, str]:
#     return {"status": "ok"}


# @app.get("/health/ready")
# async def health_ready() -> dict[str, str]:
#     return {"status": "ok"}

from fastapi import FastAPI
from app.util.config import settings
app = FastAPI(title=settings.app_name, debug=settings.app_debug)

@app.get("/health")
async def health_check():
    return {
        "status":"ok",
        "app_env":settings.app_env,
        "database_configured":bool(settings.database_url),
        }