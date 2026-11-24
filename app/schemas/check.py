from datetime import datetime

from pydantic import BaseModel, HttpUrl


class RunCheckRequest(BaseModel):
    url: HttpUrl
    method: str = "GET"
    expected_status_codes: list[int] = [200]
    keyword: str | None = None
    timeout_seconds: int = 30


class RunCheckResponse(BaseModel):
    status: str
    response_time_ms: int
    checked_at: datetime
    error_message: str | None = None
