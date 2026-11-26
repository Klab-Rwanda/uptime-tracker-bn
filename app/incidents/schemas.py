from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.incidents.service import MAX_NOTE_LENGTH

IncidentStatusFilter = Literal["open", "acknowledged", "resolved", "active"]


class IncidentResponse(BaseModel):
    id: UUID
    organization_id: UUID
    monitor_id: UUID
    status: str
    title: str
    cause: str | None
    started_at: datetime
    acknowledged_at: datetime | None
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime


class IncidentEventResponse(BaseModel):
    id: UUID
    type: str
    message: str | None
    actor_user_id: UUID | None
    created_at: datetime


class IncidentDetailResponse(IncidentResponse):
    timeline: list[IncidentEventResponse]


class IncidentEnvelope(BaseModel):
    data: IncidentResponse


class IncidentDetailEnvelope(BaseModel):
    data: IncidentDetailResponse


class IncidentListEnvelope(BaseModel):
    data: list[IncidentResponse]


class IncidentEventEnvelope(BaseModel):
    data: IncidentEventResponse


class ResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Optional: what fixed it, shown on the timeline
    note: str | None = Field(default=None, max_length=MAX_NOTE_LENGTH)


class NoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = Field(min_length=1, max_length=MAX_NOTE_LENGTH)
