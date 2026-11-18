from fastapi import FastAPI

app = FastAPI(title="UpTimeTracker API", version="0.1.0")


@app.get("/health/live")
async def health_live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
async def health_ready() -> dict[str, str]:
    return {"status": "ok"}
