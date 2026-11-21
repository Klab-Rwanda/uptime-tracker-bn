import hashlib
import hmac
import logging
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.errors import APIError, invalid_credentials
from app.auth.models import AuthCode, AuthSession, RefreshToken, User
from app.auth.schemas import RegisterRequest, TokenData
from app.auth.security import (
    create_access_token,
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    refresh_token_expiry,
    verify_password,
)
from app.notifications.brevo import EmailDeliveryError, send_email
from app.util.config import get_settings

_EMAIL_VERIFICATION = "email_verification"
_PASSWORD_RESET = "password_reset"
_MAX_CODE_ATTEMPTS = 5
_CODE_COOLDOWN_SECONDS = 60
_MAX_CODES_PER_HOUR = 5
logger = logging.getLogger(__name__)


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _token_data(user_id: UUID, session_id: UUID, refresh_token: str) -> TokenData:
    access_token, expires_in = create_access_token(
        user_id=user_id,
        auth_session_id=session_id,
    )
    return TokenData(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
    )


def _new_email_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _email_code_hash(user_id: UUID, purpose: str, code: str) -> bytes:
    secret = get_settings().jwt_secret.get_secret_value().encode("utf-8")
    message = f"{purpose}:{user_id}:{code}".encode()
    return hmac.new(secret, message, hashlib.sha256).digest()


async def _issue_email_code(
    session: AsyncSession,
    user: User,
    purpose: str,
) -> bool:
    now = datetime.now(UTC)
    window_start = now - timedelta(hours=1)
    locked_user = await session.scalar(
        select(User).where(User.id == user.id).with_for_update()
    )
    if locked_user is None or (
        purpose == _EMAIL_VERIFICATION and locked_user.is_email_verified
    ):
        return False

    recent_count = await session.scalar(
        select(func.count(AuthCode.id)).where(
            AuthCode.user_id == locked_user.id,
            AuthCode.purpose == purpose,
            AuthCode.created_at >= window_start,
        )
    )
    latest_created_at = await session.scalar(
        select(AuthCode.created_at)
        .where(
            AuthCode.user_id == locked_user.id,
            AuthCode.purpose == purpose,
        )
        .order_by(AuthCode.created_at.desc())
        .limit(1)
    )
    if latest_created_at is not None and (
        now - _utc_datetime(latest_created_at)
    ).total_seconds() < _CODE_COOLDOWN_SECONDS:
        return False
    if recent_count is not None and recent_count >= _MAX_CODES_PER_HOUR:
        return False

    await session.execute(
        update(AuthCode)
        .where(
            AuthCode.user_id == locked_user.id,
            AuthCode.purpose == purpose,
            AuthCode.consumed_at.is_(None),
        )
        .values(consumed_at=now)
    )
    code = _new_email_code()
    auth_code = AuthCode(
        user_id=locked_user.id,
        purpose=purpose,
        code_hash=_email_code_hash(locked_user.id, purpose, code),
        expires_at=now + timedelta(minutes=get_settings().email_code_ttl_minutes),
    )
    session.add(auth_code)
    await session.commit()

    action = (
        "verify your email address"
        if purpose == _EMAIL_VERIFICATION
        else "reset your password"
    )
    try:
        await send_email(
            "otp_code",
            recipient=locked_user.email,
            parameters={
                "to_name": locked_user.full_name,
                "otp_code": code,
                "action": action,
                "expires_in_minutes": str(get_settings().email_code_ttl_minutes),
            },
        )
    except EmailDeliveryError as error:
        logger.warning("Auth email delivery failed: %s", error)
        async with session.begin():
            await session.execute(
                update(AuthCode)
                .where(AuthCode.id == auth_code.id)
                .values(consumed_at=datetime.now(UTC))
            )
        raise APIError(
            503,
            "email_delivery_failed",
            "We could not send the email. Please try again later.",
        ) from error
    return True


async def _consume_email_code(
    session: AsyncSession,
    *,
    email: str,
    purpose: str,
    code: str,
) -> User | None:
    now = datetime.now(UTC)
    verified_user: User | None = None
    async with session.begin():
        user = await session.scalar(
            select(User).where(User.email == email).with_for_update()
        )
        if user is not None:
            auth_code = await session.scalar(
                select(AuthCode)
                .where(
                    AuthCode.user_id == user.id,
                    AuthCode.purpose == purpose,
                    AuthCode.consumed_at.is_(None),
                )
                .order_by(AuthCode.created_at.desc())
                .limit(1)
                .with_for_update()
            )
            if auth_code is not None:
                valid = (
                    _utc_datetime(auth_code.expires_at) > now
                    and auth_code.attempts < _MAX_CODE_ATTEMPTS
                    and hmac.compare_digest(
                        auth_code.code_hash,
                        _email_code_hash(user.id, purpose, code),
                    )
                )
                if valid:
                    auth_code.consumed_at = now
                    verified_user = user
                else:
                    if _utc_datetime(auth_code.expires_at) <= now:
                        auth_code.consumed_at = now
                    elif auth_code.attempts < _MAX_CODE_ATTEMPTS:
                        auth_code.attempts += 1
                        if auth_code.attempts >= _MAX_CODE_ATTEMPTS:
                            auth_code.consumed_at = now
    return verified_user


async def register_user(session: AsyncSession, request: RegisterRequest) -> User:
    user = User(
        email=str(request.email),
        password_hash=hash_password(request.password),
        full_name=request.full_name or str(request.email).split("@", maxsplit=1)[0],
    )
    try:
        async with session.begin():
            session.add(user)
            await session.flush()
            await session.refresh(user)
    except IntegrityError as error:
        await session.rollback()
        existing_user_id = await session.scalar(
            select(User.id).where(User.email == str(request.email))
        )
        if existing_user_id is not None:
            raise APIError(
                409, "email_in_use", "An account with this email already exists"
            ) from error
        raise
    return user


async def send_verification_code(session: AsyncSession, user: User) -> None:
    await _issue_email_code(session, user, _EMAIL_VERIFICATION)


async def resend_verification_code(session: AsyncSession, email: str) -> None:
    user = await session.scalar(select(User).where(User.email == email))
    if user is None or user.is_email_verified:
        return
    await _issue_email_code(session, user, _EMAIL_VERIFICATION)


async def verify_email(session: AsyncSession, email: str, code: str) -> None:
    user = await _consume_email_code(
        session,
        email=email,
        purpose=_EMAIL_VERIFICATION,
        code=code,
    )
    if user is None:
        raise APIError(400, "invalid_or_expired_code", "The code is invalid or expired")
    user.is_email_verified = True
    await session.commit()


async def send_password_reset_code(session: AsyncSession, email: str) -> None:
    user = await session.scalar(select(User).where(User.email == email))
    if user is not None and user.is_active:
        await _issue_email_code(session, user, _PASSWORD_RESET)


async def reset_password(
    session: AsyncSession,
    *,
    email: str,
    code: str,
    new_password: str,
) -> None:
    user = await _consume_email_code(
        session,
        email=email,
        purpose=_PASSWORD_RESET,
        code=code,
    )
    if user is None or not user.is_active:
        raise APIError(400, "invalid_or_expired_code", "The code is invalid or expired")

    now = datetime.now(UTC)
    user.password_hash = hash_password(new_password)
    await session.execute(
        update(AuthSession)
        .where(
            AuthSession.user_id == user.id,
            AuthSession.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    await session.execute(
        update(RefreshToken)
        .where(
            RefreshToken.user_id == user.id,
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    await session.commit()


async def login(
    session: AsyncSession,
    email: str,
    password: str,
) -> TokenData:
    user = await session.scalar(select(User).where(User.email == email))
    user_id = user.id if user is not None else None
    password_hash = user.password_hash if user is not None else None
    user_is_active = user.is_active if user is not None else False
    email_is_verified = user.is_email_verified if user is not None else False
    await session.rollback()

    password_matches = verify_password(password, password_hash)
    if user_id is None or not password_matches or not user_is_active:
        raise invalid_credentials()
    if not email_is_verified:
        raise APIError(
            403,
            "email_not_verified",
            "Verify your email address before logging in",
        )

    now = datetime.now(UTC)
    expires_at = refresh_token_expiry(now=now)
    raw_refresh_token = new_refresh_token()
    session_id = uuid4()
    async with session.begin():
        current_user = await session.scalar(
            select(User).where(User.id == user_id).with_for_update()
        )
        if current_user is None or not current_user.is_active:
            raise invalid_credentials()
        auth_session = AuthSession(
            id=session_id,
            user_id=current_user.id,
            expires_at=expires_at,
            last_used_at=now,
        )
        session.add(auth_session)
        session.add(
            RefreshToken(
                user_id=current_user.id,
                auth_session_id=session_id,
                token_hash=hash_refresh_token(raw_refresh_token),
                expires_at=expires_at,
            )
        )
    return _token_data(user_id, session_id, raw_refresh_token)


async def refresh(
    session: AsyncSession,
    raw_refresh_token: str,
) -> TokenData:
    now = datetime.now(UTC)
    token_hash = hash_refresh_token(raw_refresh_token)
    result: TokenData | None = None
    should_reject = False

    async with session.begin():
        stored_token = await session.scalar(
            select(RefreshToken)
            .where(RefreshToken.token_hash == token_hash)
            .with_for_update()
        )
        if stored_token is None:
            raise invalid_credentials()

        auth_session = await session.scalar(
            select(AuthSession)
            .where(AuthSession.id == stored_token.auth_session_id)
            .with_for_update()
        )
        if auth_session is None:
            raise invalid_credentials()

        token_expired = _utc_datetime(stored_token.expires_at) <= now
        session_expired = _utc_datetime(auth_session.expires_at) <= now
        replayed = stored_token.revoked_at is not None
        if replayed or session_expired or token_expired or auth_session.revoked_at:
            if replayed or session_expired or token_expired:
                auth_session.revoked_at = auth_session.revoked_at or now
                await session.execute(
                    update(RefreshToken)
                    .where(
                        RefreshToken.auth_session_id == auth_session.id,
                        RefreshToken.revoked_at.is_(None),
                    )
                    .values(revoked_at=now)
                )
            should_reject = True
        else:
            user = await session.scalar(
                select(User).where(User.id == auth_session.user_id).with_for_update()
            )
            if user is None or not user.is_active:
                auth_session.revoked_at = now
                should_reject = True
            else:
                replacement_raw_token = new_refresh_token()
                replacement = RefreshToken(
                    user_id=user.id,
                    auth_session_id=auth_session.id,
                    token_hash=hash_refresh_token(replacement_raw_token),
                    expires_at=auth_session.expires_at,
                )
                session.add(replacement)
                await session.flush()
                stored_token.revoked_at = now
                stored_token.last_used_at = now
                stored_token.replaced_by_token_id = replacement.id
                auth_session.last_used_at = now
                result = _token_data(user.id, auth_session.id, replacement_raw_token)

    if should_reject or result is None:
        raise invalid_credentials()
    return result


async def logout(session: AsyncSession, auth_session_id: UUID) -> None:
    now = datetime.now(UTC)
    await session.execute(
        update(AuthSession)
        .where(
            AuthSession.id == auth_session_id,
            AuthSession.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    await session.execute(
        update(RefreshToken)
        .where(
            RefreshToken.auth_session_id == auth_session_id,
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    await session.commit()
