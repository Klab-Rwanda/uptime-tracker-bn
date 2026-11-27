"""What an API key may do, for the request that is using it.

An API key belongs to ONE organization and carries scopes, but it authenticates
as the user who created it. Without these checks it would get that user's
access to every organization they belong to.

get_current_auth() records the key's grant for the current request, and
_get_org_membership() (used by every endpoint) refuses any other organization.
Endpoints that need a specific scope call require_scope().
"""

import json
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from app.auth.errors import APIError

if TYPE_CHECKING:
    from app.auth.dependencies import AuthenticatedUser


# Every scope an endpoint checks. A key may only be given these, so a typo
# ("monitor:read") is refused instead of silently granting nothing.
KNOWN_SCOPES = frozenset(
    {
        "monitors:read",
        "monitors:write",
        "projects:read",
        "projects:write",
        "organizations:read",
        "incidents:read",
        "incidents:write",
        "notification_channels:read",
        "notification_channels:write",
        "status_pages:read",
        "status_pages:write",
        "maintenance:read",
        "maintenance:write",
        "audit_logs:read",
    }
)


@dataclass(frozen=True)
class ApiKeyGrant:
    key_id: UUID
    organization_id: UUID
    scopes: frozenset[str]


# Set on every authentication, so a value can never carry over between requests
_current_grant: ContextVar[ApiKeyGrant | None] = ContextVar("api_key_grant", default=None)


def parse_scopes(raw: str | None) -> frozenset[str]:
    """Scopes are stored as a JSON list. Anything unreadable grants nothing."""
    try:
        values = json.loads(raw or "[]")
    except ValueError:
        return frozenset()
    if not isinstance(values, list):
        return frozenset()
    return frozenset(value for value in values if isinstance(value, str))


def set_current_grant(grant: ApiKeyGrant | None) -> None:
    _current_grant.set(grant)


def current_grant() -> ApiKeyGrant | None:
    return _current_grant.get()


def check_grant_organization(organization_id: UUID) -> None:
    """Refuse an organization the current API key doesn't belong to.

    Answers like a missing organization, so a key can't probe which other
    organizations exist.
    """
    grant = _current_grant.get()
    if grant is not None and grant.organization_id != organization_id:
        raise APIError(404, "organization_not_found", "Organization not found")


def require_scope(current: "AuthenticatedUser", scope: str) -> None:
    """Allow user logins always, and API keys only with `scope`."""
    grant = current.api_key
    if grant is not None and scope not in grant.scopes:
        raise APIError(
            403,
            "insufficient_scope",
            f"This API key needs the '{scope}' scope",
        )


def forbid_api_key(current: "AuthenticatedUser") -> None:
    """For actions only a person may take: managing API keys, members and
    ownership. A key that could do these could give itself more power."""
    if current.api_key is not None:
        raise APIError(
            403,
            "api_key_not_allowed",
            "This action needs a user login: API keys can't use it",
        )
