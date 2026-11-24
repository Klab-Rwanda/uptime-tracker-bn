from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


class MonitorCreate(BaseModel):
    organization_id: UUID
    project_id: UUID | None = None
    name: str = Field(min_length=1, max_length=255)
    type: Literal["http", "https", "tcp", "dns", "push"]
    target: str = Field(min_length=1)
    method: str | None = Field(default="GET", max_length=10)
    interval_seconds: int = Field(default=60, ge=10, le=86400)
    timeout_seconds: int = Field(default=10, ge=1, le=120)
    retries: int = Field(default=2, ge=0, le=10)
    retry_interval_seconds: int = Field(default=30, ge=5, le=3600)
    accepted_status_codes: list[int] | None = None
    expected_keyword: str | None = None
    regions: list[str] | None = None
    notification_channel_ids: list[str] | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Monitor name cannot be blank")
        return cleaned

    @field_validator("method")
    @classmethod
    def normalize_method(cls, value: str | None) -> str | None:
        if value is None:
            return "GET"
        cleaned = value.strip().upper()
        if cleaned not in ("GET", "POST", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS"):
            raise ValueError("Invalid HTTP method")
        return cleaned

    @model_validator(mode="after")
    def validate_target(self) -> "MonitorCreate":
        target = self.target.strip()
        monitor_type = self.type.lower()

        if monitor_type in ("http", "https"):
            parsed = urlsplit(target)
            if parsed.scheme not in ("http", "https"):
                raise ValueError("Target must be a valid HTTP or HTTPS URL")
            if not parsed.netloc:
                raise ValueError("Target URL must include a host")
        elif monitor_type == "tcp":
            if ":" not in target:
                raise ValueError("TCP target must be formatted as host:port")
            host, port_str = target.rsplit(":", 1)
            if not host.strip():
                raise ValueError("TCP target must include a valid host")
            try:
                port = int(port_str)
                if not (1 <= port <= 65535):
                    raise ValueError
            except ValueError:
                raise ValueError("TCP target port must be between 1 and 65535")
        elif monitor_type == "dns" and ("://" in target or "/" in target or not target):
            raise ValueError("DNS target must be a valid hostname or domain")

        return self


class MonitorUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    project_id: UUID | None = None
    target: str | None = Field(default=None, min_length=1)
    method: str | None = Field(default=None, max_length=10)
    interval_seconds: int | None = Field(default=None, ge=10, le=86400)
    timeout_seconds: int | None = Field(default=None, ge=1, le=120)
    retries: int | None = Field(default=None, ge=0, le=10)
    retry_interval_seconds: int | None = Field(default=None, ge=5, le=3600)
    accepted_status_codes: list[int] | None = None
    expected_keyword: str | None = None
    regions: list[str] | None = None
    notification_channel_ids: list[str] | None = None
    active: bool | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Monitor name cannot be blank")
        return cleaned

    @field_validator("method")
    @classmethod
    def normalize_method(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip().upper()
        if cleaned not in ("GET", "POST", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS"):
            raise ValueError("Invalid HTTP method")
        return cleaned


class MonitorResponse(BaseModel):
    id: UUID
    organization_id: UUID
    project_id: UUID | None = None
    name: str
    type: str
    target: str
    method: str | None = None
    status: str
    interval_seconds: int
    timeout_seconds: int
    retries: int
    retry_interval_seconds: int
    accepted_status_codes: list[int] | None = None
    expected_keyword: str | None = None
    regions: list[str] | None = None
    notification_channel_ids: list[str] | None = None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class MonitorEnvelope(BaseModel):
    data: MonitorResponse


class MonitorListEnvelope(BaseModel):
    data: list[MonitorResponse]


class MonitorStatusResponse(BaseModel):
    id: UUID
    status: str
    is_active: bool


class MonitorStatusEnvelope(BaseModel):
    data: MonitorStatusResponse


class MonitorTestResponse(BaseModel):
    monitor_id: UUID
    status: str
    message: str


class MonitorTestEnvelope(BaseModel):
    data: MonitorTestResponse
