from typing import Annotated

from fastapi import APIRouter, Depends

from app.auth.dependencies import AuthenticatedUser, get_current_auth
from app.checks.http_check import HTTPCheck
from app.schemas.check import RunCheckRequest, RunCheckResponse
from app.util.config import get_settings

router = APIRouter(prefix="/checks", tags=["checks"])

CurrentAuth = Annotated[AuthenticatedUser, Depends(get_current_auth)]


@router.post("/run", response_model=RunCheckResponse)
async def run_check(body: RunCheckRequest, current: CurrentAuth) -> RunCheckResponse:
    """Run one ad-hoc HTTP check.

    Requires a logged-in user, and the target gets the same SSRF protection as
    monitors: without both, anyone could use our servers to fetch internal
    addresses or to send traffic to other sites.
    """
    check = HTTPCheck(
        monitor_id="adhoc",
        target=str(body.url),
        method=body.method,
        expected_status_codes=body.expected_status_codes,
        keyword=body.keyword,
        timeout_seconds=body.timeout_seconds,
        allow_private=get_settings().monitor_allow_private_targets,
    )
    result = await check.run()
    return RunCheckResponse(
        status=result.status,
        response_time_ms=result.response_time_ms,
        checked_at=result.checked_at,
        error_message=result.error_message,
    )
