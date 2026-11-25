import secrets
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.main import app
from app.util.config import get_settings
from app.util.database import Base, get_db_session
from app.util.redis import get_redis_client

pytestmark = pytest.mark.asyncio
sent_emails: list[tuple[str, str, dict[str, str]]] = []


class FakeRedis:
    def __init__(self, attempts: int = 1) -> None:
        self.attempts = attempts

    async def eval(self, *_: object) -> int:
        return self.attempts


@pytest_asyncio.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("JWT_SECRET", secrets.token_urlsafe(48))
    monkeypatch.setenv("APP_FRONTEND_URL", "https://app.example.test")
    get_settings.cache_clear()
    sent_emails.clear()

    async def fake_send_email(kind: str, *, recipient: str, parameters: dict[str, str]) -> None:
        sent_emails.append((kind, recipient, parameters))

    monkeypatch.setattr("app.auth.service.send_email", fake_send_email)
    monkeypatch.setattr("app.organizations.service.send_email", fake_send_email)

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_redis_client] = lambda: FakeRedis()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as test_client:
        yield test_client

    app.dependency_overrides.clear()
    await engine.dispose()
    get_settings.cache_clear()
    get_redis_client.cache_clear()


async def create_account(
    client: AsyncClient,
    email: str,
    password: str = "StrongPassword123!",
) -> dict[str, str]:
    await client.post(
        "/v1/auth/register",
        json={"email": email, "password": password, "full_name": email.split("@")[0]},
    )
    _, _, parameters = next(
        msg for msg in reversed(sent_emails) if msg[1] == email.lower() and msg[0] == "otp_code"
    )
    await client.post(
        "/v1/auth/verify-email",
        json={"email": email, "code": parameters["otp_code"]},
    )
    login_resp = await client.post(
        "/v1/auth/login",
        json={"email": email, "password": password},
    )
    return login_resp.json()["data"]


def bearer(tokens: dict[str, str] | str) -> dict[str, str]:
    if isinstance(tokens, dict):
        return {"Authorization": f"Bearer {tokens['access_token']}"}
    return {"Authorization": f"Bearer {tokens}"}


async def test_api_keys_lifecycle_and_authentication(client: AsyncClient) -> None:
    owner_tokens = await create_account(client, "key_owner@example.com")
    member_tokens = await create_account(client, "key_member@example.com")

    # Create Org
    org_resp = await client.post(
        "/v1/organizations",
        json={"name": "API Key Org"},
        headers=bearer(owner_tokens),
    )
    assert org_resp.status_code == 201
    org_id = org_resp.json()["data"]["id"]

    # Invite regular member
    await client.post(
        f"/v1/organizations/{org_id}/invitations",
        json={"email": "key_member@example.com", "role": "member"},
        headers=bearer(owner_tokens),
    )
    msg = next(m for m in reversed(sent_emails) if m[0] == "invite" and m[1] == "key_member@example.com")
    token = msg[2]["invite_url"].split("#token=")[1]
    await client.post("/v1/invitations/accept", json={"token": token}, headers=bearer(member_tokens))

    # Member cannot create API keys (403)
    member_create = await client.post(
        "/v1/api-keys",
        json={"organization_id": org_id, "name": "Member Key"},
        headers=bearer(member_tokens),
    )
    assert member_create.status_code == 403

    # Owner creates API key
    create_resp = await client.post(
        "/v1/api-keys",
        json={
            "organization_id": org_id,
            "name": "Terraform integration",
            "scopes": ["monitors:read", "monitors:write"],
        },
        headers=bearer(owner_tokens),
    )
    assert create_resp.status_code == 201
    key_data = create_resp.json()["data"]
    raw_api_key = key_data["key"]
    key_id = key_data["id"]
    assert raw_api_key.startswith("sk_live_")
    assert key_data["name"] == "Terraform integration"

    # List API keys (prefix only, not full secret)
    list_resp = await client.get(
        f"/v1/api-keys?organization_id={org_id}",
        headers=bearer(owner_tokens),
    )
    assert list_resp.status_code == 200
    keys = list_resp.json()["data"]
    assert len(keys) == 1
    assert "key" not in keys[0]
    assert keys[0]["key_prefix"].startswith("sk_live_")

    # Authenticate to API using the API Key
    monitors_resp = await client.get(
        f"/v1/monitors?organization_id={org_id}",
        headers=bearer(raw_api_key),
    )
    assert monitors_resp.status_code == 200

    # Member cannot revoke key
    member_revoke = await client.delete(
        f"/v1/api-keys/{key_id}",
        headers=bearer(member_tokens),
    )
    assert member_revoke.status_code == 403

    # Owner revokes key
    owner_revoke = await client.delete(
        f"/v1/api-keys/{key_id}",
        headers=bearer(owner_tokens),
    )
    assert owner_revoke.status_code == 204

    # Using revoked API key fails with 401
    revoked_call = await client.get(
        f"/v1/monitors?organization_id={org_id}",
        headers=bearer(raw_api_key),
    )
    assert revoked_call.status_code == 401
