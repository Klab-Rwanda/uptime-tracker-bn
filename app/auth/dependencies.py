import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api_keys.models import ApiKey
from app.auth.api_key_access import ApiKeyGrant, parse_scopes, set_current_grant
from app.auth.errors import APIError
from app.auth.models import AuthSession, User
from app.auth.security import decode_access_token
from app.util.database import get_db_session

_bearer_scheme = HTTPBearer(auto_error=False)
BearerCredentials = Annotated[
    HTTPAuthorizationCredentials | None,
    Depends(_bearer_scheme),
]
DatabaseSession = Annotated[AsyncSession, Depends(get_db_session)]


@dataclass(frozen=True)
class AuthenticatedUser:
    user: User
    session_id: UUID
    # Set when the request uses an API key: its organization and scopes
    api_key: ApiKeyGrant | None = None


async def get_current_auth(
    credentials: BearerCredentials,
    session: DatabaseSession,
) -> AuthenticatedUser:
    set_current_grant(None)  # never inherit a grant from earlier work in this context
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise APIError(
            401,
            "unauthenticated",
            "Authentication is required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    raw_token = credentials.credentials.strip()

    # Handle API Key authentication
    if raw_token.startswith("sk_live_"):
        key_hash = hashlib.sha256(raw_token.encode("utf-8")).digest()
        api_key = await session.scalar(
            select(ApiKey).where(
                ApiKey.key_hash == key_hash,
                ApiKey.revoked_at.is_(None),
            )
        )
        if api_key is None:
            raise APIError(
                401,
                "invalid_token",
                "The API key is invalid or revoked",
                headers={"WWW-Authenticate": "Bearer"},
            )
        api_key.last_used_at = datetime.now(UTC)
        await session.commit()
        user = await session.get(User, api_key.created_by_user_id)
        if user is None or not user.is_active:
            raise APIError(
                401,
                "invalid_token",
                "The API key owner account is inactive or not found",
                headers={"WWW-Authenticate": "Bearer"},
            )
        grant = ApiKeyGrant(
            key_id=api_key.id,
            organization_id=api_key.organization_id,
            scopes=parse_scopes(api_key.scopes),
        )
        set_current_grant(grant)
        return AuthenticatedUser(user=user, session_id=api_key.id, api_key=grant)

    try:
        user_id, auth_session_id = decode_access_token(raw_token)
    except (jwt.InvalidTokenError, ValueError) as error:
        raise APIError(
            401,
            "invalid_token",
            "The access token is invalid or expired",
            headers={"WWW-Authenticate": "Bearer"},
        ) from error

    now = datetime.now(UTC)
    auth_session = await session.scalar(
        select(AuthSession).where(
            AuthSession.id == auth_session_id,
            AuthSession.user_id == user_id,
            AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > now,
        )
    )
    if auth_session is None:
        raise APIError(
            401,
            "invalid_token",
            "The access token is invalid or expired",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = await session.scalar(
        select(User).where(User.id == user_id, User.is_active.is_(True))
    )
    if user is None:
        raise APIError(
            401,
            "invalid_token",
            "The access token is invalid or expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return AuthenticatedUser(user=user, session_id=auth_session_id)
