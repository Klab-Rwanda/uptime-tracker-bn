from fastapi import APIRouter

from app.checks.http_check import HTTPCheck
from app.schemas.check import RunCheckRequest, RunCheckResponse

router = APIRouter(prefix="/checks", tags=["checks"])


@router.post("/run", response_model=RunCheckResponse)
async def run_check(body: RunCheckRequest):
    check = HTTPCheck(
        monitor_id="adhoc",
        target=str(body.url),
        method=body.method,
        expected_status_codes=body.expected_status_codes,
        keyword=body.keyword,
        timeout_seconds=body.timeout_seconds,
    )
    result = await check.run()
    return RunCheckResponse(
        status=result.status,
        response_time_ms=result.response_time_ms,
        checked_at=result.checked_at,
        error_message=result.error_message,
    )
