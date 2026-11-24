import hashlib
import json
import secrets
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api_keys.models import ApiKey
from app.api_keys.schemas import ApiKeyResponse
from app.auth.dependencies import AuthenticatedUser
from app.auth.errors import APIError
from app.organizations.service import _get_org_membership

ADMIN_ROLES = ("owner", "admin")


def _deserialize_scopes(raw: str) -> list[str]:
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data
        return [str(data)]
    except (json.JSONDecodeError, TypeError):
        return ["monitors:read", "monitors:write"]


def to_api_key_response(key: ApiKey) -> ApiKeyResponse:
    return ApiKeyResponse(
        id=key.id,
        name=key.name,
        key_prefix=key.key_prefix,
        scopes=_deserialize_scopes(key.scopes),
        created_at=key.created_at,
        last_used_at=key.last_used_at,
    )


async def create_api_key(
    session: AsyncSession,
    *,
    organization_id: UUID,
    name: str,
    scopes: list[str],
    current: AuthenticatedUser,
) -> tuple[ApiKey, str]:
    _, membership = await _get_org_membership(
        session,
        organization_id=organization_id,
        user_id=current.user.id,
    )
    if membership.role not in ADMIN_ROLES:
        raise APIError(
            403,
            "forbidden",
            "An owner or administrator role is required to create API keys",
        )

    raw_token = f"sk_live_{secrets.token_urlsafe(32)}"
    key_prefix = raw_token[:12] + "..."
    key_hash = hashlib.sha256(raw_token.encode("utf-8")).digest()

    api_key = ApiKey(
        organization_id=organization_id,
        name=name,
        key_prefix=key_prefix,
        key_hash=key_hash,
        scopes=json.dumps(scopes),
        created_by_user_id=current.user.id,
    )
    session.add(api_key)
    await session.commit()
    await session.refresh(api_key)
    return api_key, raw_token


async def list_api_keys(
    session: AsyncSession,
    *,
    organization_id: UUID,
    current: AuthenticatedUser,
) -> list[ApiKey]:
    await _get_org_membership(
        session,
        organization_id=organization_id,
        user_id=current.user.id,
    )
    stmt = (
        select(ApiKey)
        .where(
            ApiKey.organization_id == organization_id,
            ApiKey.revoked_at.is_(None),
        )
        .order_by(ApiKey.created_at.desc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def revoke_api_key(
    session: AsyncSession,
    *,
    key_id: UUID,
    current: AuthenticatedUser,
) -> None:
    api_key = await session.get(ApiKey, key_id)
    if api_key is None or api_key.revoked_at is not None:
        raise APIError(404, "api_key_not_found", "API key not found")

    _, membership = await _get_org_membership(
        session,
        organization_id=api_key.organization_id,
        user_id=current.user.id,
    )
    if membership.role not in ADMIN_ROLES:
        raise APIError(
            403,
            "forbidden",
            "An owner or administrator role is required to revoke API keys",
        )

    api_key.revoked_at = datetime.now(UTC)
    await session.commit()


async def verify_api_key(
    session: AsyncSession,
    raw_token: str,
) -> ApiKey | None:
    key_hash = hashlib.sha256(raw_token.encode("utf-8")).digest()
    stmt = select(ApiKey).where(
        ApiKey.key_hash == key_hash,
        ApiKey.revoked_at.is_(None),
    )
    api_key = await session.scalar(stmt)
    if api_key is not None:
        api_key.last_used_at = datetime.now(UTC)
        await session.commit()
    return api_key
