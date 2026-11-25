from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class ApiKeyCreate(BaseModel):
    organization_id: UUID
    name: str = Field(min_length=1, max_length=255)
    scopes: list[str] = Field(
        default=["monitors:read", "monitors:write"],
        min_length=1,
    )

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("API key name cannot be blank")
        return cleaned


class ApiKeyCreatedResponse(BaseModel):
    id: UUID
    name: str
    key: str
    scopes: list[str]
    created_at: datetime


class ApiKeyCreatedEnvelope(BaseModel):
    data: ApiKeyCreatedResponse


class ApiKeyResponse(BaseModel):
    id: UUID
    name: str
    key_prefix: str
    scopes: list[str]
    created_at: datetime
    last_used_at: datetime | None = None


class ApiKeyListEnvelope(BaseModel):
    data: list[ApiKeyResponse]
