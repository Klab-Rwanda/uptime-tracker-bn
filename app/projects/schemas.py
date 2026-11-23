from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class ProjectCreate(BaseModel):
    organization_id: UUID
    name: str = Field(min_length=1, max_length=255)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Project name cannot be blank")
        return normalized


class ProjectUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=255)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Project name cannot be blank")
        return normalized


class ProjectResponse(BaseModel):
    id: UUID
    organization_id: UUID
    name: str
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ProjectEnvelope(BaseModel):
    data: ProjectResponse


class ProjectListEnvelope(BaseModel):
    data: list[ProjectResponse]
