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


async def test_project_crud_and_rbac(client: AsyncClient) -> None:
    owner_tokens = await create_account(client, "project_owner@example.com")
    editor_tokens = await create_account(client, "project_editor@example.com")
    viewer_tokens = await create_account(client, "project_viewer@example.com")
    outsider_tokens = await create_account(client, "project_outsider@example.com")

    # Owner creates organization
    org_resp = await client.post(
        "/v1/organizations",
        json={"name": "Project Org"},
        headers=bearer(owner_tokens),
    )
    assert org_resp.status_code == 201
    org_id = org_resp.json()["data"]["id"]

    # Invite editor and viewer
    for email, role, tokens in [
        ("project_editor@example.com", "editor", editor_tokens),
        ("project_viewer@example.com", "viewer", viewer_tokens),
    ]:
        await client.post(
            f"/v1/organizations/{org_id}/invitations",
            json={"email": email, "role": role},
            headers=bearer(owner_tokens),
        )
        msg = next(m for m in reversed(sent_emails) if m[0] == "invite" and m[1] == email)
        token = msg[2]["invite_url"].split("#token=")[1]
        accept = await client.post(
            "/v1/organizations/invitations/accept",
            json={"token": token},
            headers=bearer(tokens),
        )
        assert accept.status_code == 200

    # Viewer CANNOT create project (403)
    viewer_create = await client.post(
        "/v1/projects",
        json={"organization_id": org_id, "name": "Viewer Project"},
        headers=bearer(viewer_tokens),
    )
    assert viewer_create.status_code == 403

    # Outsider CANNOT create project (404/403)
    outsider_create = await client.post(
        "/v1/projects",
        json={"organization_id": org_id, "name": "Outsider Project"},
        headers=bearer(outsider_tokens),
    )
    assert outsider_create.status_code in (403, 404)

    # Editor CAN create project (201)
    editor_create = await client.post(
        "/v1/projects",
        json={"organization_id": org_id, "name": "Production"},
        headers=bearer(editor_tokens),
    )
    assert editor_create.status_code == 201
    project_data = editor_create.json()["data"]
    assert project_data["name"] == "Production"
    assert project_data["organization_id"] == org_id
    project_id = project_data["id"]

    # Duplicate name in same org rejected (409)
    dup = await client.post(
        "/v1/projects",
        json={"organization_id": org_id, "name": "Production"},
        headers=bearer(owner_tokens),
    )
    assert dup.status_code == 409

    # List projects (all org members can list)
    list_resp = await client.get(
        f"/v1/projects?organization_id={org_id}",
        headers=bearer(viewer_tokens),
    )
    assert list_resp.status_code == 200
    assert len(list_resp.json()["data"]) == 1
    assert list_resp.json()["data"][0]["name"] == "Production"

    # Outsider CANNOT list projects
    outsider_list = await client.get(
        f"/v1/projects?organization_id={org_id}",
        headers=bearer(outsider_tokens),
    )
    assert outsider_list.status_code in (403, 404)

    # Get project by ID
    get_resp = await client.get(
        f"/v1/projects/{project_id}",
        headers=bearer(viewer_tokens),
    )
    assert get_resp.status_code == 200
    assert get_resp.json()["data"]["id"] == project_id

    # Outsider CANNOT get project
    outsider_get = await client.get(
        f"/v1/projects/{project_id}",
        headers=bearer(outsider_tokens),
    )
    assert outsider_get.status_code in (403, 404)

    # Viewer CANNOT update project (403)
    viewer_update = await client.patch(
        f"/v1/projects/{project_id}",
        json={"name": "Hacked"},
        headers=bearer(viewer_tokens),
    )
    assert viewer_update.status_code == 403

    # Editor CAN update project (200)
    editor_update = await client.patch(
        f"/v1/projects/{project_id}",
        json={"name": "Production Updated"},
        headers=bearer(editor_tokens),
    )
    assert editor_update.status_code == 200
    assert editor_update.json()["data"]["name"] == "Production Updated"

    # Editor CANNOT delete project (403 - requires owner or admin)
    editor_delete = await client.delete(
        f"/v1/projects/{project_id}",
        headers=bearer(editor_tokens),
    )
    assert editor_delete.status_code == 403

    # Owner CAN delete project (204)
    owner_delete = await client.delete(
        f"/v1/projects/{project_id}",
        headers=bearer(owner_tokens),
    )
    assert owner_delete.status_code == 204

    # Project is gone
    get_deleted = await client.get(
        f"/v1/projects/{project_id}",
        headers=bearer(owner_tokens),
    )
    assert get_deleted.status_code == 404
