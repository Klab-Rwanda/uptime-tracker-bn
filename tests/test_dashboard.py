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


def bearer(tokens: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def test_dashboard_summary_aggregation(client: AsyncClient) -> None:
    owner_tokens = await create_account(client, "dash_owner@example.com")
    outsider_tokens = await create_account(client, "dash_outsider@example.com")

    # Create organization
    org_resp = await client.post(
        "/v1/organizations",
        json={"name": "Dashboard Org"},
        headers=bearer(owner_tokens),
    )
    org_id = org_resp.json()["data"]["id"]

    # Create two projects
    p1 = await client.post(
        "/v1/projects",
        json={"organization_id": org_id, "name": "Frontend Web"},
        headers=bearer(owner_tokens),
    )
    p1_id = p1.json()["data"]["id"]

    p2 = await client.post(
        "/v1/projects",
        json={"organization_id": org_id, "name": "Backend APIs"},
        headers=bearer(owner_tokens),
    )
    p2_id = p2.json()["data"]["id"]

    # Create monitors under P1 and P2
    m1 = await client.post(
        "/v1/monitors",
        json={
            "organization_id": org_id,
            "project_id": p1_id,
            "name": "Landing Page",
            "type": "http",
            "target": "https://landing.example.com",
        },
        headers=bearer(owner_tokens),
    )
    m1_id = m1.json()["data"]["id"]

    await client.post(
        "/v1/monitors",
        json={
            "organization_id": org_id,
            "project_id": p2_id,
            "name": "Core Auth API",
            "type": "http",
            "target": "https://auth.example.com/health",
        },
        headers=bearer(owner_tokens),
    )

    # Pause m1
    await client.post(f"/v1/monitors/{m1_id}/pause", headers=bearer(owner_tokens))

    # Fetch dashboard summary
    summary_resp = await client.get(
        f"/v1/dashboard/summary?organization_id={org_id}",
        headers=bearer(owner_tokens),
    )
    assert summary_resp.status_code == 200
    data = summary_resp.json()["data"]

    assert data["total_monitors"] == 2
    assert data["total_projects"] == 2
    assert data["status_counts"]["paused"] == 1
    assert data["status_counts"]["pending"] == 1

    # Check project breakdown
    p1_summary = next(p for p in data["projects"] if p["id"] == p1_id)
    assert p1_summary["name"] == "Frontend Web"
    assert p1_summary["total_monitors"] == 1
    assert p1_summary["status_counts"]["paused"] == 1

    p2_summary = next(p for p in data["projects"] if p["id"] == p2_id)
    assert p2_summary["name"] == "Backend APIs"
    assert p2_summary["total_monitors"] == 1
    assert p2_summary["status_counts"]["pending"] == 1

    # Outsider cannot fetch dashboard summary
    outsider_resp = await client.get(
        f"/v1/dashboard/summary?organization_id={org_id}",
        headers=bearer(outsider_tokens),
    )
    assert outsider_resp.status_code in (403, 404)
