from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthenticatedUser, get_current_auth, get_user_login
from app.auth.models import User
from app.auth.rate_limit import enforce_login_rate_limit
from app.auth.schemas import (
    EmailActionResponse,
    EmailRequest,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    ResetPasswordRequest,
    TokenResponse,
    UserResponse,
    VerifyEmailRequest,
)
from app.auth.service import (
    login,
    logout,
    refresh,
    register_user,
    resend_verification_code,
    reset_password,
    send_password_reset_code,
    send_verification_code,
    verify_email,
)
from app.util.database import get_db_session
from app.util.redis import get_redis_client

router = APIRouter(prefix="/v1/auth", tags=["authentication"])
DatabaseSession = Annotated[AsyncSession, Depends(get_db_session)]
CurrentAuth = Annotated[AuthenticatedUser, Depends(get_current_auth)]
UserLogin = Annotated[AuthenticatedUser, Depends(get_user_login)]


@router.post(
    "/register",
    response_model=EmailActionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register(
    request: RegisterRequest,
    session: DatabaseSession,
) -> EmailActionResponse:
    user = await register_user(session, request)
    await send_verification_code(session, user)
    return EmailActionResponse(
        data={
            "email": user.email,
            "message": "A verification code has been sent to your email",
        }
    )


@router.post(
    "/verify-email",
    response_model=EmailActionResponse,
)
async def verify_email_endpoint(
    request: VerifyEmailRequest,
    session: DatabaseSession,
) -> EmailActionResponse:
    await verify_email(session, str(request.email), request.code)
    return EmailActionResponse(data={"message": "Email verified"})


@router.post(
    "/resend-verification",
    response_model=EmailActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def resend_verification_endpoint(
    request: EmailRequest,
    session: DatabaseSession,
) -> EmailActionResponse:
    await resend_verification_code(session, str(request.email))
    return EmailActionResponse(
        data={"message": "If eligible, a verification code will be sent"}
    )


@router.post(
    "/forgot-password",
    response_model=EmailActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def forgot_password_endpoint(
    request: EmailRequest,
    session: DatabaseSession,
) -> EmailActionResponse:
    await send_password_reset_code(session, str(request.email))
    return EmailActionResponse(
        data={"message": "If the account exists, a reset code will be sent"}
    )


@router.post(
    "/reset-password",
    response_model=EmailActionResponse,
)
async def reset_password_endpoint(
    request: ResetPasswordRequest,
    session: DatabaseSession,
) -> EmailActionResponse:
    await reset_password(
        session,
        email=str(request.email),
        code=request.code,
        new_password=request.new_password,
    )
    return EmailActionResponse(
        data={"message": "Password reset successfully; please log in again"}
    )


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
    current: UserLogin,
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
