from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

MAX_DURATION = timedelta(days=30)
MAX_MONITORS = 100


class WindowState(StrEnum):
    UPCOMING = "upcoming"
    ACTIVE = "active"
    PAST = "past"


def _clean_name(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("Name cannot be blank")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in cleaned):
        raise ValueError("Name must not contain control characters")
    return cleaned


def _unique_monitors(value: list[UUID]) -> list[UUID]:
    if len(set(value)) != len(value):
        raise ValueError("A monitor can be listed only once")
    return value


def check_times(starts_at: datetime, ends_at: datetime) -> None:
    """Shared by create and update (where one time may come from the database)."""
    if ends_at <= starts_at:
        raise ValueError("ends_at must be after starts_at")
    if ends_at - starts_at > MAX_DURATION:
        raise ValueError(f"A maintenance window can last at most {MAX_DURATION.days} days")


class MaintenanceWindowCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organization_id: UUID
    name: str = Field(min_length=1, max_length=255)
    # AwareDatetime: times must say their timezone ("...Z" or "+02:00"), so
    # "01:00" can't silently mean a different hour than the user meant
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    monitor_ids: list[UUID] = Field(min_length=1, max_length=MAX_MONITORS)
    suppress_notifications: bool = True

    @field_validator("name")
    @classmethod
    def check_name(cls, value: str) -> str:
        return _clean_name(value)

    @field_validator("monitor_ids")
    @classmethod
    def check_monitors(cls, value: list[UUID]) -> list[UUID]:
        return _unique_monitors(value)

    @model_validator(mode="after")
    def check_window(self) -> "MaintenanceWindowCreate":
        check_times(self.starts_at, self.ends_at)
        return self


class MaintenanceWindowUpdate(BaseModel):
    """Only the fields that are sent change. monitor_ids replaces the list."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    starts_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None
    monitor_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=MAX_MONITORS)
    suppress_notifications: bool | None = None

    @field_validator("name")
    @classmethod
    def check_name(cls, value: str | None) -> str | None:
        return None if value is None else _clean_name(value)

    @field_validator("monitor_ids")
    @classmethod
    def check_monitors(cls, value: list[UUID] | None) -> list[UUID] | None:
        return None if value is None else _unique_monitors(value)

    @model_validator(mode="after")
    def check_update(self) -> "MaintenanceWindowUpdate":
        if not self.model_fields_set:
            raise ValueError("Nothing to update")
        for field in self.model_fields_set:
            if getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class MaintenanceWindowResponse(BaseModel):
    id: UUID
    organization_id: UUID
    name: str
    starts_at: datetime
    ends_at: datetime
    state: WindowState
    monitor_ids: list[UUID]
    suppress_notifications: bool
    created_by_user_id: UUID | None
    created_at: datetime
    updated_at: datetime


class MaintenanceWindowEnvelope(BaseModel):
    data: MaintenanceWindowResponse


class MaintenanceWindowListEnvelope(BaseModel):
    data: list[MaintenanceWindowResponse]
