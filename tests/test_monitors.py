import secrets
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.main import app
from app.notification_channels.crypto import ChannelCipher
from app.notification_channels.dependencies import get_channel_cipher
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

    async def fake_send_email(
        kind: str,
        *,
        recipient: str,
        parameters: dict[str, str],
    ) -> None:
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
    test_cipher = ChannelCipher([Fernet.generate_key().decode()])
    app.dependency_overrides[get_channel_cipher] = lambda: test_cipher
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
        msg
        for msg in reversed(sent_emails)
        if msg[1] == email.lower() and msg[0] == "otp_code"
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


async def setup_org_and_members(client: AsyncClient):
    owner_tokens = await create_account(client, "owner_mon@example.com")
    editor_tokens = await create_account(client, "editor_mon@example.com")
    viewer_tokens = await create_account(client, "viewer_mon@example.com")
    outsider_tokens = await create_account(client, "outsider_mon@example.com")

    # Create Org
    org_resp = await client.post(
        "/v1/organizations",
        json={"name": "Monitor Testing Org", "slug": "mon-org"},
        headers=bearer(owner_tokens),
    )
    assert org_resp.status_code == 201
    org_id = org_resp.json()["data"]["id"]

    # Invite editor and viewer
    for email, role, tokens in [
        ("editor_mon@example.com", "editor", editor_tokens),
        ("viewer_mon@example.com", "viewer", viewer_tokens),
    ]:
        await client.post(
            f"/v1/organizations/{org_id}/invitations",
            json={"email": email, "role": role},
            headers=bearer(owner_tokens),
        )
        msg = next(m for m in reversed(sent_emails) if m[0] == "invite" and m[1] == email)
        token = msg[2]["invite_url"].split("#token=")[1]
        await client.post(
            "/v1/invitations/accept",
            json={"token": token},
            headers=bearer(tokens),
        )

    # Create a project
    proj_resp = await client.post(
        "/v1/projects",
        json={"organization_id": org_id, "name": "Main Platform"},
        headers=bearer(owner_tokens),
    )
    assert proj_resp.status_code == 201
    project_id = proj_resp.json()["data"]["id"]

    return {
        "org_id": org_id,
        "project_id": project_id,
        "owner": owner_tokens,
        "editor": editor_tokens,
        "viewer": viewer_tokens,
        "outsider": outsider_tokens,
    }


async def test_create_and_get_monitor_types(client: AsyncClient) -> None:
    data = await setup_org_and_members(client)
    channel_resp = await client.post(
        "/v1/notification-channels",
        json={
            "organization_id": data["org_id"],
            "name": "On-call email",
            "type": "email",
            "config": {"recipients": ["oncall@example.com"]},
        },
        headers=bearer(data["editor"]),
    )
    assert channel_resp.status_code == 201
    channel_id = channel_resp.json()["data"]["id"]

    # 1. HTTP monitor
    http_payload = {
        "organization_id": data["org_id"],
        "project_id": data["project_id"],
        "name": "Customer API",
        "type": "http",
        "target": "https://api.customer.com/health",
        "method": "GET",
        "interval_seconds": 60,
        "timeout_seconds": 10,
        "retries": 2,
        "retry_interval_seconds": 30,
        "accepted_status_codes": [200, 201],
        "expected_keyword": "ok",
        "regions": ["eu-west-1", "af-south-1"],
        "notification_channel_ids": [channel_id],
    }
    http_resp = await client.post(
        "/v1/monitors",
        json=http_payload,
        headers=bearer(data["editor"]),
    )
    assert http_resp.status_code == 201
    created_http = http_resp.json()["data"]
    assert created_http["name"] == "Customer API"
    assert created_http["type"] == "http"
    assert created_http["target"] == "https://api.customer.com/health"
    assert created_http["status"] == "pending"
    assert created_http["is_active"] is True
    assert created_http["accepted_status_codes"] == [200, 201]
    assert created_http["regions"] == ["eu-west-1", "af-south-1"]
    assert created_http["notification_channel_ids"] == [channel_id]
    monitor_id = created_http["id"]

    # 2. TCP monitor
    tcp_payload = {
        "organization_id": data["org_id"],
        "project_id": data["project_id"],
        "name": "Database Port Check",
        "type": "tcp",
        "target": "db.customer.com:5432",
        "interval_seconds": 30,
    }
    tcp_resp = await client.post(
        "/v1/monitors",
        json=tcp_payload,
        headers=bearer(data["editor"]),
    )
    assert tcp_resp.status_code == 201
    assert tcp_resp.json()["data"]["type"] == "tcp"

    # 3. DNS monitor
    dns_payload = {
        "organization_id": data["org_id"],
        "name": "Domain Resolution Check",
        "type": "dns",
        "target": "customer.com",
    }
    dns_resp = await client.post(
        "/v1/monitors",
        json=dns_payload,
        headers=bearer(data["editor"]),
    )
    assert dns_resp.status_code == 201
    assert dns_resp.json()["data"]["type"] == "dns"

    # 4. Get HTTP monitor by ID
    get_resp = await client.get(
        f"/v1/monitors/{monitor_id}",
        headers=bearer(data["viewer"]),
    )
    assert get_resp.status_code == 200
    fetched = get_resp.json()["data"]
    assert fetched["id"] == monitor_id
    assert fetched["name"] == "Customer API"
    assert fetched["expected_keyword"] == "ok"


async def test_monitor_validation_errors(client: AsyncClient) -> None:
    data = await setup_org_and_members(client)

    # Invalid HTTP target URL
    bad_url_resp = await client.post(
        "/v1/monitors",
        json={
            "organization_id": data["org_id"],
            "name": "Bad URL",
            "type": "http",
            "target": "not-a-valid-url",
        },
        headers=bearer(data["editor"]),
    )
    assert bad_url_resp.status_code == 422

    # Invalid TCP port format
    bad_tcp_resp = await client.post(
        "/v1/monitors",
        json={
            "organization_id": data["org_id"],
            "name": "Bad TCP",
            "type": "tcp",
            "target": "hostname_without_port",
        },
        headers=bearer(data["editor"]),
    )
    assert bad_tcp_resp.status_code == 422

    # Invalid interval (< 10)
    low_interval_resp = await client.post(
        "/v1/monitors",
        json={
            "organization_id": data["org_id"],
            "name": "Too fast",
            "type": "http",
            "target": "https://api.example.com",
            "interval_seconds": 2,
        },
        headers=bearer(data["editor"]),
    )
    assert low_interval_resp.status_code == 422


async def test_monitor_rbac_and_tenant_isolation(client: AsyncClient) -> None:
    data = await setup_org_and_members(client)

    # Viewer CANNOT create monitor (403)
    viewer_create = await client.post(
        "/v1/monitors",
        json={
            "organization_id": data["org_id"],
            "name": "Viewer Try",
            "type": "http",
            "target": "https://api.example.com",
        },
        headers=bearer(data["viewer"]),
    )
    assert viewer_create.status_code == 403

    # Editor creates monitor
    create_resp = await client.post(
        "/v1/monitors",
        json={
            "organization_id": data["org_id"],
            "project_id": data["project_id"],
            "name": "API Service",
            "type": "http",
            "target": "https://api.example.com/v1",
        },
        headers=bearer(data["editor"]),
    )
    assert create_resp.status_code == 201
    monitor_id = create_resp.json()["data"]["id"]

    # Outsider CANNOT get monitor (404)
    outsider_get = await client.get(
        f"/v1/monitors/{monitor_id}",
        headers=bearer(data["outsider"]),
    )
    assert outsider_get.status_code in (403, 404)

    # Viewer CANNOT update monitor (403)
    viewer_update = await client.patch(
        f"/v1/monitors/{monitor_id}",
        json={"name": "Hacked Name"},
        headers=bearer(data["viewer"]),
    )
    assert viewer_update.status_code == 403

    # Editor CAN update monitor (200)
    editor_update = await client.patch(
        f"/v1/monitors/{monitor_id}",
        json={"name": "API Service Updated", "interval_seconds": 120},
        headers=bearer(data["editor"]),
    )
    assert editor_update.status_code == 200
    assert editor_update.json()["data"]["name"] == "API Service Updated"
    assert editor_update.json()["data"]["interval_seconds"] == 120

    # Editor CANNOT delete monitor (403)
    editor_delete = await client.delete(
        f"/v1/monitors/{monitor_id}",
        headers=bearer(data["editor"]),
    )
    assert editor_delete.status_code == 403

    # Owner CAN delete monitor (204)
    owner_delete = await client.delete(
        f"/v1/monitors/{monitor_id}",
        headers=bearer(data["owner"]),
    )
    assert owner_delete.status_code == 204

    # Confirm deleted
    confirm_del = await client.get(
        f"/v1/monitors/{monitor_id}",
        headers=bearer(data["owner"]),
    )
    assert confirm_del.status_code == 404


async def test_pause_resume_and_filtering(client: AsyncClient) -> None:
    data = await setup_org_and_members(client)

    # Create two monitors
    m1 = await client.post(
        "/v1/monitors",
        json={
            "organization_id": data["org_id"],
            "project_id": data["project_id"],
            "name": "Monitor One",
            "type": "http",
            "target": "https://one.example.com",
        },
        headers=bearer(data["editor"]),
    )
    m1_id = m1.json()["data"]["id"]

    m2 = await client.post(
        "/v1/monitors",
        json={
            "organization_id": data["org_id"],
            "name": "Monitor Two",
            "type": "tcp",
            "target": "two.example.com:80",
        },
        headers=bearer(data["editor"]),
    )
    m2_id = m2.json()["data"]["id"]

    # Pause Monitor One
    pause_resp = await client.post(
        f"/v1/monitors/{m1_id}/pause",
        headers=bearer(data["editor"]),
    )
    assert pause_resp.status_code == 200
    assert pause_resp.json()["data"]["status"] == "paused"
    assert pause_resp.json()["data"]["is_active"] is False

    # Filter by status=paused
    filter_paused = await client.get(
        f"/v1/monitors?organization_id={data['org_id']}&status=paused",
        headers=bearer(data["viewer"]),
    )
    assert filter_paused.status_code == 200
    paused_ids = [m["id"] for m in filter_paused.json()["data"]]
    assert m1_id in paused_ids
    assert m2_id not in paused_ids

    # Resume Monitor One
    resume_resp = await client.post(
        f"/v1/monitors/{m1_id}/resume",
        headers=bearer(data["editor"]),
    )
    assert resume_resp.status_code == 200
    assert resume_resp.json()["data"]["status"] == "pending"
    assert resume_resp.json()["data"]["is_active"] is True

    # Filter by type=tcp
    filter_tcp = await client.get(
        f"/v1/monitors?organization_id={data['org_id']}&type=tcp",
        headers=bearer(data["viewer"]),
    )
    assert filter_tcp.status_code == 200
    tcp_ids = [m["id"] for m in filter_tcp.json()["data"]]
    assert m2_id in tcp_ids
    assert m1_id not in tcp_ids

    # Test monitor endpoint
    test_resp = await client.post(
        f"/v1/monitors/{m2_id}/test",
        headers=bearer(data["editor"]),
    )
    assert test_resp.status_code == 200
    assert test_resp.json()["data"]["status"] == "queued"
