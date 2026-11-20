from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_validator


class RegisterRequest(BaseModel):
    email: EmailStr = Field(max_length=255)
    password: str = Field(min_length=12, max_length=128)
    full_name: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: EmailStr) -> str:
        return str(value).strip().lower()

    @field_validator("full_name")
    @classmethod
    def normalize_full_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("Full name cannot be blank")
        return normalized


class LoginRequest(BaseModel):
    email: EmailStr = Field(max_length=255)
    password: str = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: EmailStr) -> str:
        return str(value).strip().lower()


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=32, max_length=512)


class EmailRequest(BaseModel):
    email: EmailStr = Field(max_length=255)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: EmailStr) -> str:
        return str(value).strip().lower()


class VerifyEmailRequest(EmailRequest):
    code: str = Field(pattern=r"^\d{6}$")


class ResetPasswordRequest(VerifyEmailRequest):
    new_password: str = Field(min_length=12, max_length=128)


class UserResponse(BaseModel):
    id: UUID
    email: EmailStr
    full_name: str
    created_at: datetime


class TokenData(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class TokenResponse(BaseModel):
    data: TokenData


class EmailActionResponse(BaseModel):
    data: dict[str, str]
