import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.util.config import Settings, get_settings

JWT_ALGORITHM = "HS256"
_password_hasher = PasswordHasher()
_dummy_password_hash = _password_hasher.hash(secrets.token_urlsafe(32))


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    encoded_hash = password_hash or _dummy_password_hash
    try:
        verified = _password_hasher.verify(encoded_hash, password)
    except (InvalidHashError, VerificationError, VerifyMismatchError):
        return False
    return verified and password_hash is not None


def new_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def hash_refresh_token(refresh_token: str) -> bytes:
    return hashlib.sha256(refresh_token.encode("utf-8")).digest()


def create_access_token(
    *,
    user_id: UUID,
    auth_session_id: UUID,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> tuple[str, int]:
    token_settings = settings or get_settings()
    current_time = now or datetime.now(UTC)
    issued_at = int(current_time.timestamp())
    expires_in = token_settings.access_token_ttl_seconds
    payload = {
        "sub": str(user_id),
        "sid": str(auth_session_id),
        "jti": secrets.token_urlsafe(16),
        "typ": "access",
        "iat": issued_at,
        "exp": issued_at + expires_in,
        "iss": token_settings.jwt_issuer,
        "aud": token_settings.jwt_audience,
    }
    token = jwt.encode(
        payload,
        token_settings.jwt_secret.get_secret_value(),
        algorithm=JWT_ALGORITHM,
    )
    return token, expires_in


def decode_access_token(
    token: str,
    settings: Settings | None = None,
) -> tuple[UUID, UUID]:
    token_settings = settings or get_settings()
    payload = jwt.decode(
        token,
        token_settings.jwt_secret.get_secret_value(),
        algorithms=[JWT_ALGORITHM],
        issuer=token_settings.jwt_issuer,
        audience=token_settings.jwt_audience,
        options={
            "require": ["sub", "sid", "jti", "typ", "iat", "exp", "iss", "aud"],
        },
    )
    if payload.get("typ") != "access":
        raise jwt.InvalidTokenError("Not an access token")
    return UUID(payload["sub"]), UUID(payload["sid"])


def refresh_token_expiry(
    settings: Settings | None = None,
    now: datetime | None = None,
) -> datetime:
    token_settings = settings or get_settings()
    current_time = now or datetime.now(UTC)
    return current_time + timedelta(days=token_settings.refresh_token_ttl_days)
