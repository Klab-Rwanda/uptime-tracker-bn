import secrets
from collections.abc import AsyncIterator
from uuid import UUID

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.main import app
from app.models.monitor import Monitor
from app.notification_channels import crypto
from app.notification_channels.crypto import ChannelCipher, ChannelCryptoError
from app.notification_channels.dependencies import get_channel_cipher
from app.notification_channels.models import NotificationChannel
from app.util.config import get_settings
from app.util.database import Base, get_db_session
from app.util.redis import get_redis_client
from tests.helpers.tenant import assert_tenant_isolated

pytestmark = pytest.mark.asyncio
sent_emails: list[tuple[str, str, dict[str, str]]] = []

URL = "https://hooks.acme.com/services/T0/B0/token-abc"
CIPHER = ChannelCipher([Fernet.generate_key().decode()])


class FakeRedis:
    async def eval(self, *_: object) -> int:
        return 1


@pytest_asyncio.fixture
async def sessions(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("JWT_SECRET", secrets.token_urlsafe(48))
    monkeypatch.setenv("APP_FRONTEND_URL", "https://app.example.test")
    monkeypatch.setenv("WEBHOOK_ALLOW_PRIVATE_TARGETS", "false")
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
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_redis_client] = lambda: FakeRedis()
    app.dependency_overrides[get_channel_cipher] = lambda: CIPHER
    yield factory
    app.dependency_overrides.clear()
    await engine.dispose()
    get_settings.cache_clear()
    get_redis_client.cache_clear()


@pytest_asyncio.fixture
async def client(sessions) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def bearer(tokens: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def create_account(client: AsyncClient, email: str) -> dict[str, str]:
    password = "StrongPassword123!"
    await client.post(
        "/v1/auth/register",
        json={"email": email, "password": password, "full_name": email.split("@")[0]},
    )
    _, _, params = next(
        m for m in reversed(sent_emails) if m[1] == email.lower() and m[0] == "otp_code"
    )
    await client.post("/v1/auth/verify-email", json={"email": email, "code": params["otp_code"]})
    login = await client.post("/v1/auth/login", json={"email": email, "password": password})
    return login.json()["data"]


async def create_org(client: AsyncClient, tokens: dict[str, str], slug: str) -> str:
    resp = await client.post(
        "/v1/organizations", json={"name": slug.title(), "slug": slug}, headers=bearer(tokens)
    )
    assert resp.status_code == 201
    return resp.json()["data"]["id"]


async def invite(client, org_id, owner, email, role, tokens) -> None:
    await client.post(
        f"/v1/organizations/{org_id}/invitations",
        json={"email": email, "role": role},
        headers=bearer(owner),
    )
    msg = next(m for m in reversed(sent_emails) if m[0] == "invite" and m[1] == email)
    token = msg[2]["invite_url"].split("#token=")[1]
    await client.post("/v1/invitations/accept", json={"token": token}, headers=bearer(tokens))


@pytest_asyncio.fixture
async def org(client: AsyncClient) -> dict:
    owner = await create_account(client, "owner_ch@example.com")
    editor = await create_account(client, "editor_ch@example.com")
    viewer = await create_account(client, "viewer_ch@example.com")
    outsider = await create_account(client, "outsider_ch@example.com")
    org_id = await create_org(client, owner, "channels-org")
    await invite(client, org_id, owner, "editor_ch@example.com", "editor", editor)
    await invite(client, org_id, owner, "viewer_ch@example.com", "viewer", viewer)
    other_org_id = await create_org(client, outsider, "other-org")
    return {
        "org_id": org_id,
        "other_org_id": other_org_id,
        "owner": owner,
        "editor": editor,
        "viewer": viewer,
        "outsider": outsider,
    }


def webhook_body(org_id: str, name: str = "On-call hook") -> dict:
    return {"organization_id": org_id, "name": name, "type": "webhook", "config": {"webhook_url": URL}}


def email_body(org_id: str, name: str = "On-call email") -> dict:
    return {
        "organization_id": org_id,
        "name": name,
        "type": "email",
        "config": {"recipients": ["Ops@Example.com"]},
    }


async def create_channel(client, org, body=None, who="editor") -> dict:
    resp = await client.post(
        "/v1/notification-channels",
        json=body or webhook_body(org["org_id"]),
        headers=bearer(org[who]),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


async def stored_config(sessions, channel_id: str) -> dict:
    async with sessions() as session:
        channel = await session.get(NotificationChannel, UUID(channel_id))
        assert channel is not None
        return {"raw": channel.config_encrypted, "config": CIPHER.decrypt(channel.config_encrypted)}


# --- create -------------------------------------------------------------------


async def test_create_webhook_returns_the_secret_once(client, org, sessions):
    resp = await client.post(
        "/v1/notification-channels", json=webhook_body(org["org_id"]), headers=bearer(org["editor"])
    )
    assert resp.status_code == 201
    assert resp.headers["cache-control"] == "no-store"
    data = resp.json()["data"]
    assert data["type"] == "webhook"
    assert data["config_summary"] == {"url_host": "hooks.acme.com"}
    assert data["signing_secret"].startswith("whsec_")
    assert "token-abc" not in resp.text  # the URL path may hold a token

    stored = await stored_config(sessions, data["id"])
    assert stored["config"] == {"webhook_url": URL, "secret": data["signing_secret"]}
    assert "token-abc" not in stored["raw"] and "whsec_" not in stored["raw"]  # encrypted at rest


async def test_create_email_has_no_secret(client, org):
    data = await create_channel(client, org, email_body(org["org_id"]))
    assert data["config_summary"] == {"recipients": ["ops@example.com"]}
    assert "signing_secret" not in data


async def test_viewer_cannot_create(client, org):
    resp = await client.post(
        "/v1/notification-channels", json=webhook_body(org["org_id"]), headers=bearer(org["viewer"])
    )
    assert resp.status_code == 403


async def test_duplicate_name_is_a_conflict(client, org):
    await create_channel(client, org)
    resp = await client.post(
        "/v1/notification-channels", json=webhook_body(org["org_id"]), headers=bearer(org["editor"])
    )
    assert resp.status_code == 409


async def test_same_name_is_fine_in_another_organization(client, org):
    await create_channel(client, org)
    await create_channel(client, org, webhook_body(org["other_org_id"]), who="outsider")


@pytest.mark.parametrize(
    "config",
    [
        {"webhook_url": "https://169.254.169.254/latest/meta-data/"},
        {"webhook_url": "http://hooks.acme.com/x"},
        {"webhook_url": "https://localhost/x"},
        {"webhook_url": URL, "secret": "mine"},
    ],
)
async def test_unsafe_or_bad_configs_are_rejected(client, org, config):
    body = {**webhook_body(org["org_id"]), "config": config}
    resp = await client.post("/v1/notification-channels", json=body, headers=bearer(org["editor"]))
    assert resp.status_code == 422


async def test_requires_authentication(client, org):
    resp = await client.post("/v1/notification-channels", json=webhook_body(org["org_id"]))
    assert resp.status_code == 401


async def test_misconfigured_encryption_is_a_503(client, org, monkeypatch):
    del app.dependency_overrides[get_channel_cipher]

    def broken():
        raise ChannelCryptoError("CHANNEL_ENCRYPTION_KEYS is not configured")

    monkeypatch.setattr("app.notification_channels.dependencies.get_cipher", broken)
    resp = await client.post(
        "/v1/notification-channels", json=webhook_body(org["org_id"]), headers=bearer(org["editor"])
    )
    assert resp.status_code == 503
    crypto.get_cipher.cache_clear()


# --- list ---------------------------------------------------------------------


async def test_any_member_can_list_without_secrets(client, org):
    await create_channel(client, org)
    await create_channel(client, org, email_body(org["org_id"]))
    resp = await client.get(
        f"/v1/notification-channels?organization_id={org['org_id']}", headers=bearer(org["viewer"])
    )
    assert resp.status_code == 200
    names = [c["name"] for c in resp.json()["data"]]
    assert names == ["On-call email", "On-call hook"]
    assert "whsec_" not in resp.text
    assert "token-abc" not in resp.text


# --- tenant isolation ----------------------------------------------------------


async def test_other_tenants_cannot_touch_channels(client, org):
    channel = await create_channel(client, org)
    outsider = bearer(org["outsider"])
    url = f"/v1/notification-channels/{channel['id']}"
    await assert_tenant_isolated(
        client, "GET", f"/v1/notification-channels?organization_id={org['org_id']}", outsider_headers=outsider
    )
    await assert_tenant_isolated(client, "PATCH", url, outsider_headers=outsider, json={"name": "Mine"})
    await assert_tenant_isolated(client, "DELETE", url, outsider_headers=outsider)
    await assert_tenant_isolated(
        client, "POST", "/v1/notification-channels", outsider_headers=outsider, json=webhook_body(org["org_id"], "x")
    )


# --- update ---------------------------------------------------------------------


async def test_rename(client, org):
    channel = await create_channel(client, org)
    resp = await client.patch(
        f"/v1/notification-channels/{channel['id']}", json={"name": "Renamed"}, headers=bearer(org["editor"])
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["name"] == "Renamed"
    assert "signing_secret" not in resp.json()["data"]


async def test_changing_the_url_keeps_the_secret(client, org, sessions):
    channel = await create_channel(client, org)
    new_url = "https://hooks.example.org/new-token"
    resp = await client.patch(
        f"/v1/notification-channels/{channel['id']}",
        json={"config": {"webhook_url": new_url}},
        headers=bearer(org["editor"]),
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["config_summary"] == {"url_host": "hooks.example.org"}
    stored = (await stored_config(sessions, channel["id"]))["config"]
    assert stored == {"webhook_url": new_url, "secret": channel["signing_secret"]}


async def test_rotating_the_secret_returns_a_new_one(client, org, sessions):
    channel = await create_channel(client, org)
    resp = await client.patch(
        f"/v1/notification-channels/{channel['id']}", json={"rotate_secret": True}, headers=bearer(org["editor"])
    )
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    new_secret = resp.json()["data"]["signing_secret"]
    assert new_secret.startswith("whsec_") and new_secret != channel["signing_secret"]
    stored = (await stored_config(sessions, channel["id"]))["config"]
    assert stored == {"webhook_url": URL, "secret": new_secret}


async def test_email_channels_have_no_secret_to_rotate(client, org):
    channel = await create_channel(client, org, email_body(org["org_id"]))
    resp = await client.patch(
        f"/v1/notification-channels/{channel['id']}", json={"rotate_secret": True}, headers=bearer(org["editor"])
    )
    assert resp.status_code == 422


async def test_renaming_onto_an_existing_name_is_a_conflict(client, org):
    await create_channel(client, org)
    other = await create_channel(client, org, email_body(org["org_id"]))
    resp = await client.patch(
        f"/v1/notification-channels/{other['id']}", json={"name": "On-call hook"}, headers=bearer(org["editor"])
    )
    assert resp.status_code == 409


async def test_viewer_cannot_update(client, org):
    channel = await create_channel(client, org)
    resp = await client.patch(
        f"/v1/notification-channels/{channel['id']}", json={"name": "x"}, headers=bearer(org["viewer"])
    )
    assert resp.status_code == 403


# --- delete ---------------------------------------------------------------------


async def test_only_owner_or_admin_can_delete(client, org):
    channel = await create_channel(client, org)
    url = f"/v1/notification-channels/{channel['id']}"
    assert (await client.delete(url, headers=bearer(org["editor"]))).status_code == 403
    assert (await client.delete(url, headers=bearer(org["owner"]))).status_code == 204
    assert (await client.delete(url, headers=bearer(org["owner"]))).status_code == 404


async def test_deleting_a_channel_removes_it_from_monitors(client, org, sessions):
    keep = await create_channel(client, org, email_body(org["org_id"]))
    gone = await create_channel(client, org)
    monitor = await client.post(
        "/v1/monitors",
        json={
            "organization_id": org["org_id"],
            "name": "API",
            "type": "http",
            "target": "https://api.example.com/health",
            "notification_channel_ids": [keep["id"], gone["id"]],
        },
        headers=bearer(org["editor"]),
    )
    assert monitor.status_code == 201, monitor.text

    await client.delete(f"/v1/notification-channels/{gone['id']}", headers=bearer(org["owner"]))
    async with sessions() as session:
        stored = await session.scalar(select(Monitor.notification_channel_ids))
    assert stored is not None and gone["id"] not in stored and keep["id"] in stored


# --- monitors may only use their own organization's channels ---------------------


async def test_monitor_cannot_use_another_tenants_channel(client, org):
    foreign = await create_channel(client, org, webhook_body(org["other_org_id"]), who="outsider")
    body = {
        "organization_id": org["org_id"],
        "name": "API",
        "type": "http",
        "target": "https://api.example.com/health",
        "notification_channel_ids": [foreign["id"]],
    }
    created = await client.post("/v1/monitors", json=body, headers=bearer(org["editor"]))
    assert created.status_code == 404

    body["notification_channel_ids"] = []
    monitor = await client.post("/v1/monitors", json=body, headers=bearer(org["editor"]))
    assert monitor.status_code == 201
    updated = await client.patch(
        f"/v1/monitors/{monitor.json()['data']['id']}",
        json={"notification_channel_ids": [foreign["id"]]},
        headers=bearer(org["editor"]),
    )
    assert updated.status_code == 404


async def test_monitor_rejects_malformed_channel_ids(client, org):
    body = {
        "organization_id": org["org_id"],
        "name": "API",
        "type": "http",
        "target": "https://api.example.com/health",
        "notification_channel_ids": ["chan_123"],
    }
    resp = await client.post("/v1/monitors", json=body, headers=bearer(org["editor"]))
    assert resp.status_code == 422


async def test_monitor_channel_ids_are_deduplicated(client, org):
    channel = await create_channel(client, org)
    body = {
        "organization_id": org["org_id"],
        "name": "API",
        "type": "http",
        "target": "https://api.example.com/health",
        "notification_channel_ids": [channel["id"], channel["id"].upper()],
    }
    resp = await client.post("/v1/monitors", json=body, headers=bearer(org["editor"]))
    assert resp.status_code == 201
    assert resp.json()["data"]["notification_channel_ids"] == [channel["id"]]


# --- API keys: scopes and organization binding ------------------------------------


async def make_api_key(client, org_id: str, tokens: dict, scopes: list[str]) -> dict[str, str]:
    resp = await client.post(
        "/v1/api-keys",
        json={"organization_id": org_id, "name": f"key-{len(scopes)}-{secrets.token_hex(3)}", "scopes": scopes},
        headers=bearer(tokens),
    )
    assert resp.status_code == 201, resp.text
    return {"Authorization": f"Bearer {resp.json()['data']['key']}"}


async def test_api_key_needs_channel_scopes(client, org):
    await create_channel(client, org)
    list_url = f"/v1/notification-channels?organization_id={org['org_id']}"

    monitors_key = await make_api_key(client, org["org_id"], org["owner"], ["monitors:read"])
    denied = await client.get(list_url, headers=monitors_key)
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "insufficient_scope"

    read_key = await make_api_key(client, org["org_id"], org["owner"], ["notification_channels:read"])
    assert (await client.get(list_url, headers=read_key)).status_code == 200
    write = await client.post("/v1/notification-channels", json=webhook_body(org["org_id"], "k"), headers=read_key)
    assert write.status_code == 403

    write_key = await make_api_key(client, org["org_id"], org["owner"], ["notification_channels:write"])
    created = await client.post("/v1/notification-channels", json=webhook_body(org["org_id"], "k"), headers=write_key)
    assert created.status_code == 201


async def test_api_key_cannot_reach_its_creators_other_organizations(client, org):
    # The owner of org A also owns org C; a key made for A must not open C
    second_org = await create_org(client, org["owner"], "second-org")
    key_for_a = await make_api_key(
        client,
        org["org_id"],
        org["owner"],
        # Every read scope, so the organization check is what stops it
        ["monitors:read", "notification_channels:read", "organizations:read"],
    )

    for url in (
        f"/v1/monitors?organization_id={second_org}",
        f"/v1/notification-channels?organization_id={second_org}",
        f"/v1/organizations/{second_org}",
    ):
        resp = await client.get(url, headers=key_for_a)
        assert resp.status_code == 404, f"{url} returned {resp.status_code}"

    # Its own organization still works, and so does the owner's normal login
    assert (await client.get(f"/v1/monitors?organization_id={org['org_id']}", headers=key_for_a)).status_code == 200
    assert (await client.get(f"/v1/organizations/{second_org}", headers=bearer(org["owner"]))).status_code == 200


# --- Slack channels -------------------------------------------------------------------

SLACK_URL = "https://hooks.slack.com/services/T0001/B0002/abcdefSECRET123"


def slack_body(org_id: str, url: str = SLACK_URL, name: str = "On-call Slack") -> dict:
    return {"organization_id": org_id, "name": name, "type": "slack", "config": {"webhook_url": url}}


async def test_create_slack_channel_never_shows_the_url(client, org, sessions):
    data = await create_channel(client, org, slack_body(org["org_id"]))
    assert data["type"] == "slack"
    assert data["config_summary"] == {"url_host": "hooks.slack.com"}
    assert data.get("signing_secret") is None  # Slack URLs carry their own secret

    listed = await client.get(f"/v1/notification-channels?organization_id={org['org_id']}", headers=bearer(org["viewer"]))
    assert "abcdefSECRET123" not in listed.text
    stored = await stored_config(sessions, data["id"])
    assert stored["config"] == {"webhook_url": SLACK_URL}  # kept, but only encrypted
    assert "abcdefSECRET123" not in stored["raw"]


@pytest.mark.parametrize(
    "config",
    [
        {"webhook_url": "https://example.com/hook"},
        {"webhook_url": "https://hooks.slack.com.evil.com/services/T0001/B0002/abc"},
        {"webhook_url": "http://hooks.slack.com/services/T0001/B0002/abc"},
        {"webhook_url": "http://127.0.0.1/services/T0001/B0002/abc"},
        {"webhook_url": SLACK_URL, "secret": "x"},
        {},
    ],
)
async def test_only_slack_webhook_urls_are_accepted(client, org, config):
    body = {**slack_body(org["org_id"]), "config": config}
    resp = await client.post("/v1/notification-channels", json=body, headers=bearer(org["editor"]))
    assert resp.status_code == 422
    assert "evil" not in resp.text


async def test_change_the_slack_url(client, org, sessions):
    channel = await create_channel(client, org, slack_body(org["org_id"]))
    new_url = "https://hooks.slack.com/services/T0001/B0003/newSECRET456"
    resp = await client.patch(
        f"/v1/notification-channels/{channel['id']}",
        json={"config": {"webhook_url": new_url}},
        headers=bearer(org["editor"]),
    )
    assert resp.status_code == 200, resp.text
    assert (await stored_config(sessions, channel["id"]))["config"] == {"webhook_url": new_url}

    bad = await client.patch(
        f"/v1/notification-channels/{channel['id']}",
        json={"config": {"webhook_url": "https://example.com/x"}},
        headers=bearer(org["editor"]),
    )
    assert bad.status_code == 422


async def test_slack_channels_have_no_secret_to_rotate(client, org):
    channel = await create_channel(client, org, slack_body(org["org_id"]))
    resp = await client.patch(
        f"/v1/notification-channels/{channel['id']}", json={"rotate_secret": True}, headers=bearer(org["editor"])
    )
    assert resp.status_code == 422
