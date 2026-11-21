from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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


async def get_current_auth(
    credentials: BearerCredentials,
    session: DatabaseSession,
) -> AuthenticatedUser:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise APIError(
            401,
            "unauthenticated",
            "Authentication is required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        user_id, auth_session_id = decode_access_token(credentials.credentials)
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
