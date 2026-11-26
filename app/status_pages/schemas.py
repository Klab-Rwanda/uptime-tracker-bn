import re
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,62}[a-z0-9])$")
# Slugs that would be confusing or clash with our own paths
RESERVED_SLUGS = frozenset({"admin", "api", "app", "status", "www", "health", "metrics", "v1"})
_LABEL = r"(?!-)[a-z0-9-]{1,63}(?<!-)"
DOMAIN_RE = re.compile(rf"^(?:{_LABEL}\.)+[a-z]{{2,63}}$")
MAX_GROUPS = 20
MAX_MONITORS = 100


def clean_slug(value: str) -> str:
    slug = value.strip().lower()
    if not SLUG_RE.fullmatch(slug):
        raise ValueError(
            "slug must be 3-64 characters: lowercase letters, digits and hyphens, "
            "not starting or ending with a hyphen"
        )
    if slug in RESERVED_SLUGS:
        raise ValueError("This slug is reserved")
    return slug


def clean_domain(value: str | None) -> str | None:
    if value is None:
        return None
    domain = value.strip().lower().rstrip(".")
    if not domain:
        return None
    if len(domain) > 253 or not DOMAIN_RE.fullmatch(domain):
        raise ValueError("custom_domain must be a host name like status.example.com")
    return domain


def _clean_text(value: str, field: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field} cannot be blank")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in cleaned):
        raise ValueError(f"{field} must not contain control characters")
    return cleaned


class GroupIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    monitor_ids: list[UUID] = Field(default_factory=list, max_length=MAX_MONITORS)

    @field_validator("name")
    @classmethod
    def check_name(cls, value: str) -> str:
        return _clean_text(value, "Group name")


def _check_groups(groups: list[GroupIn] | None) -> None:
    if groups is None:
        return
    monitor_ids = [m for g in groups for m in g.monitor_ids]
    if len(monitor_ids) > MAX_MONITORS:
        raise ValueError(f"A status page can show at most {MAX_MONITORS} monitors")
    if len(set(monitor_ids)) != len(monitor_ids):
        raise ValueError("A monitor can appear only once on a status page")


class StatusPageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organization_id: UUID
    title: str = Field(min_length=1, max_length=255)
    slug: str
    custom_domain: str | None = None
    groups: list[GroupIn] = Field(default_factory=list, max_length=MAX_GROUPS)

    @field_validator("title")
    @classmethod
    def check_title(cls, value: str) -> str:
        return _clean_text(value, "Title")

    @field_validator("slug")
    @classmethod
    def check_slug(cls, value: str) -> str:
        return clean_slug(value)

    @field_validator("custom_domain")
    @classmethod
    def check_domain(cls, value: str | None) -> str | None:
        return clean_domain(value)

    @model_validator(mode="after")
    def check_groups(self) -> "StatusPageCreate":
        _check_groups(self.groups)
        return self


class StatusPageUpdate(BaseModel):
    """Only the fields that are sent change. Send "custom_domain": null to remove
    the domain, and "groups" to replace all groups."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=255)
    slug: str | None = None
    custom_domain: str | None = None
    groups: list[GroupIn] | None = Field(default=None, max_length=MAX_GROUPS)

    @field_validator("title")
    @classmethod
    def check_title(cls, value: str | None) -> str | None:
        return None if value is None else _clean_text(value, "Title")

    @field_validator("slug")
    @classmethod
    def check_slug(cls, value: str | None) -> str | None:
        return None if value is None else clean_slug(value)

    @field_validator("custom_domain")
    @classmethod
    def check_domain(cls, value: str | None) -> str | None:
        return clean_domain(value)

    @model_validator(mode="after")
    def check(self) -> "StatusPageUpdate":
        if not self.model_fields_set:
            raise ValueError("Nothing to update")
        _check_groups(self.groups)
        return self


class GroupOut(BaseModel):
    name: str
    monitor_ids: list[UUID]


class StatusPageResponse(BaseModel):
    id: UUID
    organization_id: UUID
    title: str
    slug: str
    custom_domain: str | None
    url: str
    groups: list[GroupOut]
    created_at: datetime
    updated_at: datetime


class StatusPageEnvelope(BaseModel):
    data: StatusPageResponse


class StatusPageListEnvelope(BaseModel):
    data: list[StatusPageResponse]


# --- the public page: deliberately minimal, nothing private -------------------


class PublicMonitor(BaseModel):
    name: str
    status: str


class PublicGroup(BaseModel):
    name: str
    status: str
    monitors: list[PublicMonitor]


class PublicIncident(BaseModel):
    title: str
    status: str
    started_at: datetime


class PublicMaintenance(BaseModel):
    name: str
    status: str  # "in_progress" or "scheduled"
    starts_at: datetime
    ends_at: datetime
    monitors: list[str]  # names of the affected monitors shown on this page


class PublicPastIncident(BaseModel):
    title: str
    started_at: datetime
    resolved_at: datetime
    duration_seconds: int


class PublicStatusPage(BaseModel):
    title: str
    overall_status: str
    groups: list[PublicGroup]
    active_incidents: list[PublicIncident]
    maintenance: list[PublicMaintenance]
    incident_history: list[PublicPastIncident]
    generated_at: datetime


class PublicStatusPageEnvelope(BaseModel):
    data: PublicStatusPage
