from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_validator


class OrganizationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str | None = Field(default=None, min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Organization name cannot be blank")
        return normalized

    @field_validator("slug")
    @classmethod
    def normalize_slug(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("Organization slug cannot be blank")
        return normalized


class OrganizationUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Organization name cannot be blank")
        return normalized


class TransferOwnershipRequest(BaseModel):
    user_id: UUID


class OrganizationResponse(BaseModel):
    id: UUID
    name: str
    slug: str | None = None
    role: str
    created_at: datetime | None = None
    updated_at: datetime | None = None


class OrganizationEnvelope(BaseModel):
    data: OrganizationResponse


class OrganizationListEnvelope(BaseModel):
    data: list[OrganizationResponse]


class MemberResponse(BaseModel):
    user_id: UUID
    email: EmailStr
    full_name: str
    role: str
    created_at: datetime


class MemberListEnvelope(BaseModel):
    data: list[MemberResponse]


class InvitationCreate(BaseModel):
    email: EmailStr = Field(max_length=255)
    role: str = Field(default="editor", max_length=32)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: EmailStr) -> str:
        return str(value).strip().lower()

    @field_validator("role")
    @classmethod
    def normalize_role(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if cleaned not in ("owner", "admin", "editor", "viewer", "member"):
            raise ValueError("Invalid role")
        return cleaned


class InvitationResponse(BaseModel):
    id: UUID
    organization_id: UUID
    email: EmailStr
    role: str
    status: str
    expires_at: datetime
    created_at: datetime


class InvitationEnvelope(BaseModel):
    data: InvitationResponse


class InvitationListEnvelope(BaseModel):
    data: list[InvitationResponse]


class AcceptInvitationRequest(BaseModel):
    token: str = Field(min_length=1)


class AcceptInvitationResponse(BaseModel):
    organization_id: UUID
    organization_name: str
    role: str


class AcceptInvitationEnvelope(BaseModel):
    data: AcceptInvitationResponse


class MessageEnvelope(BaseModel):
    data: dict[str, str]
