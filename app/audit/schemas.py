from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class AuditActor(BaseModel):
    user_id: UUID | None
    email: str | None  # None once the user is deleted
    api_key_id: UUID | None  # set when the change came through an API key


class AuditLogResponse(BaseModel):
    id: UUID
    action: str
    target_type: str
    target_id: UUID | None
    actor: AuditActor
    details: dict[str, Any]
    request_id: str | None
    created_at: datetime


class AuditLogListEnvelope(BaseModel):
    data: list[AuditLogResponse]
    # Pass as ?before=... to get the next (older) page; None on the last page
    next_before: datetime | None
