from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator


class RunCheckRequest(BaseModel):
    url: HttpUrl
    # Read-only methods: this endpoint must never send writes to arbitrary sites
    method: Literal["GET", "HEAD"] = "GET"
    expected_status_codes: list[int] = Field(default=[200], min_length=1, max_length=20)
    keyword: str | None = Field(default=None, max_length=1000)
    timeout_seconds: int = Field(default=30, ge=1, le=60)

    @field_validator("expected_status_codes")
    @classmethod
    def check_codes(cls, value: list[int]) -> list[int]:
        if any(not 100 <= code <= 599 for code in value):
            raise ValueError("Status codes must be between 100 and 599")
        return value


class RunCheckResponse(BaseModel):
    status: str
    response_time_ms: int
    checked_at: datetime
    error_message: str | None = None
