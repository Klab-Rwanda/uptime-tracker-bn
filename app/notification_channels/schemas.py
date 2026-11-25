import ipaddress
import re
from datetime import datetime
from typing import Any, Literal, Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from app.services.webhook.url_safety import (
    UnsafeWebhookURL,
    check_url_syntax,
    is_public_ip,
)
from app.util.config import get_settings

# Validators raise ValueError even for wrong types (ruff TRY004 suggests TypeError):
# pydantic only turns ValueError into a 422; a TypeError would be a 500.
ChannelType = Literal["webhook", "email"]
MAX_EMAIL_RECIPIENTS = 10
MAX_EMAIL_LENGTH = 254
_EMAIL_RE = re.compile(r"[^@\s<>,;\"'\\]+@[^@\s<>,;\"'\\]+\.[^@\s<>,;\"'\\]+")


def _clean_name(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("Name cannot be blank")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in cleaned):
        raise ValueError("Name must not contain control characters")
    return cleaned


def _normalize_webhook(config: dict[str, Any], allow_private: bool) -> dict[str, Any]:
    if set(config) - {"webhook_url"}:
        raise ValueError("Unknown setting for a webhook channel")
    url = config.get("webhook_url")
    if not isinstance(url, str):
        raise ValueError("webhook_url is required")  # noqa: TRY004
    url = url.strip()
    try:
        parsed = check_url_syntax(url, allow_private=allow_private)
    except UnsafeWebhookURL as exc:
        raise ValueError(f"webhook_url: {exc}") from exc
    if not allow_private:
        # Early, friendly errors. The real check happens again at send time.
        if parsed.host == "localhost" or parsed.host.endswith(".localhost"):
            raise ValueError("webhook_url: host must be a public address")
        try:
            literal = ipaddress.ip_address(parsed.host)
        except ValueError:
            literal = None
        if literal is not None and not is_public_ip(str(literal)):
            raise ValueError("webhook_url: host must be a public address")
    return {"webhook_url": url}


def _normalize_email(config: dict[str, Any]) -> dict[str, Any]:
    if set(config) - {"recipients"}:
        raise ValueError("Unknown setting for an email channel")
    raw = config.get("recipients")
    if not isinstance(raw, list) or not raw:
        raise ValueError("recipients must be a non-empty list of email addresses")
    recipients: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ValueError("recipients must be a list of email addresses")  # noqa: TRY004
        address = item.strip().lower()
        if len(address) > MAX_EMAIL_LENGTH or not _EMAIL_RE.fullmatch(address):
            raise ValueError("recipients contains an invalid email address")
        if address not in recipients:
            recipients.append(address)
    if len(recipients) > MAX_EMAIL_RECIPIENTS:
        raise ValueError(f"at most {MAX_EMAIL_RECIPIENTS} recipients are allowed")
    return {"recipients": recipients}


def normalize_config(
    channel_type: str, config: dict[str, Any], *, allow_private: bool = False
) -> dict[str, Any]:
    """Validate and normalise a channel config. Error messages never echo input values."""
    if channel_type == "webhook":
        return _normalize_webhook(config, allow_private)
    if channel_type == "email":
        return _normalize_email(config)
    raise ValueError("Unsupported channel type")


def summarize_config(channel_type: str, config: dict[str, Any]) -> dict[str, Any]:
    """The only part of a config that API responses may show."""
    if channel_type == "webhook":
        # Webhook URLs often carry a secret token in the path, so show the host only
        return {"url_host": urlsplit(str(config.get("webhook_url", ""))).hostname or ""}
    if channel_type == "email":
        return {"recipients": list(config.get("recipients", []))}
    return {}


class ChannelCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organization_id: UUID
    name: str = Field(min_length=1, max_length=255)
    type: ChannelType
    config: dict[str, Any]

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return _clean_name(value)

    @field_validator("config")
    @classmethod
    def check_config(
        cls, value: dict[str, Any], info: ValidationInfo
    ) -> dict[str, Any]:
        channel_type = info.data.get("type")
        if channel_type is None:  # the type itself was invalid and is reported already
            return value
        return normalize_config(
            channel_type,
            value,
            allow_private=get_settings().webhook_allow_private_targets,
        )


class ChannelUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    config: dict[str, Any] | None = None
    rotate_secret: bool = False

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str | None) -> str | None:
        return None if value is None else _clean_name(value)

    @model_validator(mode="after")
    def require_a_change(self) -> Self:
        if self.name is None and self.config is None and not self.rotate_secret:
            raise ValueError("Nothing to update")
        return self


class ChannelResponse(BaseModel):
    id: UUID
    organization_id: UUID
    name: str
    type: str
    config_summary: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    # Shown once: when a webhook channel is created or its secret is rotated
    signing_secret: str | None = None


class ChannelEnvelope(BaseModel):
    data: ChannelResponse


class ChannelListEnvelope(BaseModel):
    data: list[ChannelResponse]
