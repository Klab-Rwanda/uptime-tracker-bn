from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthenticatedUser, get_current_auth
from app.auth.models import User
from app.auth.rate_limit import enforce_login_rate_limit
from app.auth.schemas import (
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)
from app.auth.service import login, logout, refresh, register_user
from app.util.database import get_db_session
from app.util.redis import get_redis_client

router = APIRouter(prefix="/v1/auth", tags=["authentication"])
DatabaseSession = Annotated[AsyncSession, Depends(get_db_session)]
CurrentAuth = Annotated[AuthenticatedUser, Depends(get_current_auth)]


@router.post(
    "/register",
    response_model=dict[str, UserResponse],
    status_code=status.HTTP_201_CREATED,
)
async def register(
    request: RegisterRequest,
    session: DatabaseSession,
) -> dict[str, UserResponse]:
    user = await register_user(session, request)
    return {
        "data": UserResponse(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            created_at=user.created_at,
        )
    }


@router.post("/login", response_model=TokenResponse)
async def login_endpoint(
    request: LoginRequest,
    session: DatabaseSession,
    http_request: Request,
    redis: Annotated[Redis, Depends(get_redis_client)],
) -> TokenResponse:
    await enforce_login_rate_limit(
        redis,
        email=str(request.email),
        client_ip=http_request.client.host if http_request.client else "unknown",
    )
    tokens = await login(session, str(request.email), request.password)
    return TokenResponse(data=tokens)


@router.post("/refresh", response_model=TokenResponse)
async def refresh_endpoint(
    request: RefreshRequest,
    session: DatabaseSession,
) -> TokenResponse:
    tokens = await refresh(session, request.refresh_token)
    return TokenResponse(data=tokens)


@router.post("/logout", response_model=dict[str, dict[str, str]])
async def logout_endpoint(
    current: CurrentAuth,
    session: DatabaseSession,
) -> dict[str, dict[str, str]]:
    await logout(session, current.session_id)
    return {"data": {"message": "Logged out"}}


@router.get("/me", response_model=dict[str, UserResponse])
async def current_user(
    current: CurrentAuth,
) -> dict[str, UserResponse]:
    user: User = current.user
    return {
        "data": UserResponse(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            created_at=user.created_at,
        )
    }
