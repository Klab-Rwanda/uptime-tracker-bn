from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class HeartbeatResponse(BaseModel):
    id: UUID
    monitor_id: UUID
    status: str  # "up", "down" or "pending"
    response_time_ms: int | None
    message: str | None  # why a check failed; None when it passed
    checked_at: datetime


class HeartbeatListMeta(BaseModel):
    # Pass as ?cursor=... for the next (older) page; None on the last page
    next_cursor: str | None


class HeartbeatListEnvelope(BaseModel):
    data: list[HeartbeatResponse]
    meta: HeartbeatListMeta


class LatestHeartbeatEnvelope(BaseModel):
    data: HeartbeatResponse | None  # None until the monitor's first check
