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
    get_settings.cache_clear()

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


async def register(client: AsyncClient, email: str = "alice@example.com") -> None:
    response = await client.post(
        "/v1/auth/register",
        json={"email": email, "password": "StrongPassword123!"},
    )
    assert response.status_code == 201


async def login(
    client: AsyncClient, email: str = "alice@example.com"
) -> dict[str, str]:
    response = await client.post(
        "/v1/auth/login",
        json={"email": email, "password": "StrongPassword123!"},
    )
    assert response.status_code == 200
    return response.json()["data"]


async def test_register_login_and_current_user(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/auth/register",
        json={
            "email": "Alice@Example.com",
            "password": "StrongPassword123!",
        },
    )

    assert response.status_code == 201
    assert response.json()["data"]["email"] == "alice@example.com"
    assert "password" not in response.json()["data"]

    tokens = await login(client)
    profile = await client.get(
        "/v1/auth/me",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )

    assert profile.status_code == 200
    assert profile.json()["data"]["email"] == "alice@example.com"


async def test_duplicate_email_and_bad_login_are_safe(client: AsyncClient) -> None:
    await register(client, "Alice@Example.com")

    duplicate = await client.post(
        "/v1/auth/register",
        json={
            "email": "alice@example.com",
            "password": "StrongPassword123!",
        },
    )
    bad_login = await client.post(
        "/v1/auth/login",
        json={"email": "unknown@example.com", "password": "WrongPassword123!"},
    )

    assert duplicate.status_code == 409
    assert bad_login.status_code == 401
    assert bad_login.json()["error"]["message"] == "Invalid email or password"


async def test_login_rate_limit_returns_429(client: AsyncClient) -> None:
    app.dependency_overrides[get_redis_client] = lambda: FakeRedis(attempts=11)

    response = await client.post(
        "/v1/auth/login",
        json={"email": "alice@example.com", "password": "StrongPassword123!"},
    )

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limit_exceeded"


async def test_refresh_rotates_tokens_and_replay_revokes_session(
    client: AsyncClient,
) -> None:
    await register(client)
    tokens = await login(client)

    rotated = await client.post(
        "/v1/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]},
    )
    assert rotated.status_code == 200
    replacement = rotated.json()["data"]
    assert replacement["refresh_token"] != tokens["refresh_token"]

    replay = await client.post(
        "/v1/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]},
    )
    profile = await client.get(
        "/v1/auth/me",
        headers={"Authorization": f"Bearer {replacement['access_token']}"},
    )

    assert replay.status_code == 401
    assert profile.status_code == 401


async def test_logout_revokes_access_and_refresh_tokens(client: AsyncClient) -> None:
    await register(client)
    tokens = await login(client)

    logout_response = await client.post(
        "/v1/auth/logout",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    profile = await client.get(
        "/v1/auth/me",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    refresh = await client.post(
        "/v1/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]},
    )

    assert logout_response.status_code == 200
    assert logout_response.json() == {"data": {"message": "Logged out"}}
    assert profile.status_code == 401
    assert refresh.status_code == 401
