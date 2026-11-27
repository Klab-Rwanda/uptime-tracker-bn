from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.auth.api_key_access import KNOWN_SCOPES


class ApiKeyCreate(BaseModel):
    organization_id: UUID
    name: str = Field(min_length=1, max_length=255)
    scopes: list[str] = Field(
        default=["monitors:read", "monitors:write"],
        min_length=1,
        max_length=len(KNOWN_SCOPES),
    )

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("API key name cannot be blank")
        return cleaned

    @field_validator("scopes")
    @classmethod
    def check_scopes(cls, value: list[str]) -> list[str]:
        # Clipped: the message must not echo arbitrarily long input
        unknown = sorted({scope[:40] for scope in set(value) - KNOWN_SCOPES})
        if unknown:
            raise ValueError(
                f"Unknown scope(s): {', '.join(unknown)}. "
                f"Allowed: {', '.join(sorted(KNOWN_SCOPES))}"
            )
        return sorted(set(value))  # no duplicates, stable order


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
