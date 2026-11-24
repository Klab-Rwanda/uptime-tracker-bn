import secrets
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.auth.errors import APIError
from app.main import app
from app.util.config import get_settings
from app.util.database import Base, get_db_session
from app.util.redis import get_redis_client
from app.websocket.manager import ConnectionManager
from app.websocket.router import _authenticate_ws

pytestmark = pytest.mark.asyncio
sent_emails: list[tuple[str, str, dict[str, str]]] = []


class FakeRedis:
    def __init__(self, attempts: int = 1) -> None:
        self.attempts = attempts

    async def eval(self, *_: object) -> int:
        return self.attempts

    async def publish(self, *_: object) -> int:
        return 1


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
    monkeypatch.setattr("app.websocket.router.sessionLocal", session_factory)

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


def bearer(tokens: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def test_websocket_manager_lifecycle() -> None:
    cm = ConnectionManager()
    org_id = uuid4()
    mock_ws = AsyncMock()

    await cm.connect(org_id, mock_ws)
    assert org_id in cm.active_connections
    assert mock_ws in cm.active_connections[org_id]

    await cm.broadcast_org_event(
        org_id,
        "monitor.status_changed",
        {"monitor_id": "mon_123", "status": "down"},
    )
    mock_ws.send_json.assert_awaited_once()
    payload = mock_ws.send_json.await_args[0][0]
    assert payload["type"] == "monitor.status_changed"
    assert payload["data"]["status"] == "down"

    cm.disconnect(org_id, mock_ws)
    assert org_id not in cm.active_connections


async def test_websocket_auth(client: AsyncClient) -> None:
    owner_tokens = await create_account(client, "ws_user@example.com")
    outsider_tokens = await create_account(client, "ws_outsider@example.com")

    # Create Org
    org_resp = await client.post(
        "/v1/organizations",
        json={"name": "WS Test Org"},
        headers=bearer(owner_tokens),
    )
    org_id = UUID(org_resp.json()["data"]["id"])

    # 1. Valid user in org authenticates
    user = await _authenticate_ws(owner_tokens["access_token"], org_id)
    assert user.email == "ws_user@example.com"

    # 2. Invalid token raises ValueError
    with pytest.raises(ValueError):
        await _authenticate_ws("bad_token", org_id)

    # 3. Outsider user not in org raises APIError/ValueError
    with pytest.raises((APIError, ValueError)):
        await _authenticate_ws(outsider_tokens["access_token"], org_id)
