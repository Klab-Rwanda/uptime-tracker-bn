from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.errors import APIError, invalid_credentials
from app.auth.models import AuthSession, RefreshToken, User
from app.auth.schemas import RegisterRequest, TokenData
from app.auth.security import (
    create_access_token,
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    refresh_token_expiry,
    verify_password,
)


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


async def login(
    session: AsyncSession,
    email: str,
    password: str,
) -> TokenData:
    user = await session.scalar(select(User).where(User.email == email))
    user_id = user.id if user is not None else None
    password_hash = user.password_hash if user is not None else None
    user_is_active = user.is_active if user is not None else False
    await session.rollback()

    password_matches = verify_password(password, password_hash)
    if user_id is None or not password_matches or not user_is_active:
        raise invalid_credentials()

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
