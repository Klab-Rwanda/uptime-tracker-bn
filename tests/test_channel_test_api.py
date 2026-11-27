import asyncio
import secrets
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.main import app
from app.notification_channels import testing
from app.notification_channels.crypto import ChannelCipher
from app.notification_channels.dependencies import (
    get_channel_cipher,
    get_test_email_provider,
    get_test_slack_sender,
    get_test_webhook_sender,
)
from app.services.email.fake import InMemoryEmailProvider
from app.services.slack.fake import InMemorySlackSender
from app.services.slack.sender import PermanentSlackError
from app.services.webhook.base import RetryableWebhookError
from app.services.webhook.fake import InMemoryWebhookSender
from app.util.config import get_settings
from app.util.database import get_db_session
from app.util.redis import get_redis_client
from tests.conftest import dispose_test_engine, make_test_engine

sent_emails: list[tuple[str, str, dict[str, str]]] = []
CIPHER = ChannelCipher([Fernet.generate_key().decode()])
WEBHOOK_URL = "https://hooks.acme.com/services/T0/secret-token-123"

SLACK_URL = "https://hooks.slack.com/services/T0001/B0002/abcdefSECRET123"


class CountingRedis:
    """Like the real INCR-with-expiry script: counts calls per key."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.down = False

    async def eval(self, script, numkeys, key, *args):
        if self.down:
            raise RedisError("connection refused")
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def env(request, tmp_path, monkeypatch) -> AsyncIterator[dict]:
    monkeypatch.setenv("JWT_SECRET", secrets.token_urlsafe(48))
    monkeypatch.setenv("APP_FRONTEND_URL", "https://app.example.test")
    get_settings.cache_clear()
    sent_emails.clear()

    async def fake_send_email(kind: str, *, recipient: str, parameters: dict[str, str]) -> None:
        sent_emails.append((kind, recipient, parameters))

    monkeypatch.setattr("app.auth.service.send_email", fake_send_email)
    monkeypatch.setattr("app.organizations.service.send_email", fake_send_email)

    engine = await make_test_engine(request.param, tmp_path)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    redis, email, webhook = CountingRedis(), InMemoryEmailProvider(), InMemoryWebhookSender()

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_redis_client] = lambda: redis
    app.dependency_overrides[get_channel_cipher] = lambda: CIPHER
    app.dependency_overrides[get_test_email_provider] = lambda: email
    app.dependency_overrides[get_test_webhook_sender] = lambda: webhook
    slack = InMemorySlackSender()
    app.dependency_overrides[get_test_slack_sender] = lambda: slack
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield {"client": client, "redis": redis, "email": email, "webhook": webhook, "slack": slack}
    app.dependency_overrides.clear()
    await dispose_test_engine(engine)
    get_settings.cache_clear()
    get_redis_client.cache_clear()


def bearer(tokens: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def create_account(client: AsyncClient, email: str) -> dict[str, str]:
    password = "StrongPassword123!"
    await client.post(
        "/v1/auth/register", json={"email": email, "password": password, "full_name": "Test"}
    )
    _, _, params = next(m for m in reversed(sent_emails) if m[1] == email.lower() and m[0] == "otp_code")
    await client.post("/v1/auth/verify-email", json={"email": email, "code": params["otp_code"]})
    return (await client.post("/v1/auth/login", json={"email": email, "password": password})).json()["data"]


async def invite(client, org_id, owner, email, role, tokens) -> None:
    await client.post(
        f"/v1/organizations/{org_id}/invitations", json={"email": email, "role": role}, headers=bearer(owner)
    )
    msg = next(m for m in reversed(sent_emails) if m[0] == "invite" and m[1] == email)
    token = msg[2]["invite_url"].split("#token=")[1]
    await client.post("/v1/invitations/accept", json={"token": token}, headers=bearer(tokens))


@pytest_asyncio.fixture
async def world(env) -> dict:
    client = env["client"]
    tag = uuid4().hex[:6]
    owner = await create_account(client, f"owner_{tag}@example.com")
    editor = await create_account(client, f"editor_{tag}@example.com")
    viewer = await create_account(client, f"viewer_{tag}@example.com")
    outsider = await create_account(client, f"out_{tag}@example.com")
    org = await client.post("/v1/organizations", json={"name": "Acme", "slug": f"acme-{tag}"}, headers=bearer(owner))
    org_id = org.json()["data"]["id"]
    await invite(client, org_id, owner, f"editor_{tag}@example.com", "editor", editor)
    await invite(client, org_id, owner, f"viewer_{tag}@example.com", "viewer", viewer)

    async def channel(kind: str, config: dict) -> str:
        resp = await client.post(
            "/v1/notification-channels",
            json={"organization_id": org_id, "name": f"{kind} channel", "type": kind, "config": config},
            headers=bearer(editor),
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["data"]["id"]

    return {
        **env,
        "org_id": org_id,
        "owner": owner,
        "editor": editor,
        "viewer": viewer,
        "outsider": outsider,
        "email_channel": await channel("email", {"recipients": ["oncall@acme.com", "ops@acme.com"]}),
        "webhook_channel": await channel("webhook", {"webhook_url": WEBHOOK_URL}),
        "slack_channel": await channel("slack", {"webhook_url": SLACK_URL}),
    }


async def run_test(world, channel_key: str, who: str = "editor", headers=None):
    return await world["client"].post(
        f"/v1/notification-channels/{world[channel_key]}/test",
        headers=headers or bearer(world[who]),
    )


# --- it really sends ------------------------------------------------------------------


async def test_email_channel_test_is_sent(world):
    resp = await run_test(world, "email_channel")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert (data["status"], data["detail"], data["provider_message_id"]) == ("sent", None, "memory-1")

    mail = world["email"].sent[0]
    assert mail.subject == "[TEST] UpTimeTracker alerts for email channel work"
    assert mail.to == ["oncall@acme.com", "ops@acme.com"]
    assert mail.idempotency_key.startswith(f"test:{world['email_channel']}:")


async def test_webhook_channel_test_is_sent_signed(world):
    resp = await run_test(world, "webhook_channel")
    assert resp.json()["data"]["status"] == "sent"
    url, event = world["webhook"].sent[0]
    assert url == WEBHOOK_URL
    assert event.event_type == "channel.test"
    assert event.data["channel_id"] == world["webhook_channel"]
    assert event.data["channel_name"] == "webhook channel"


async def test_each_test_is_a_new_message(world):
    await run_test(world, "email_channel")
    await run_test(world, "email_channel")
    keys = [m.idempotency_key for m in world["email"].sent]
    assert len(keys) == 2 and keys[0] != keys[1]


# --- failures are reported, safely ------------------------------------------------------


async def test_a_failing_endpoint_is_reported_not_raised(world):
    world["webhook"].fail_with = RetryableWebhookError("Endpoint returned 503")
    resp = await run_test(world, "webhook_channel")
    assert resp.status_code == 200
    assert resp.json()["data"] == {
        "status": "failed",
        "detail": "Endpoint returned 503",
        "provider_message_id": None,
        "sent_at": resp.json()["data"]["sent_at"],
    }


async def test_failure_details_never_leak_secrets(world):
    world["webhook"].fail_with = RuntimeError(f"connect to {WEBHOOK_URL} failed")
    resp = await run_test(world, "webhook_channel")
    assert resp.json()["data"]["detail"] == "Unexpected error (RuntimeError)"
    assert "secret-token-123" not in resp.text


async def test_missing_email_setup_is_reported(world):
    app.dependency_overrides[get_test_email_provider] = lambda: None
    resp = await run_test(world, "email_channel")
    assert resp.json()["data"]["status"] == "failed"
    assert resp.json()["data"]["detail"] == "Email delivery is not configured"


async def test_slow_endpoints_time_out(world, monkeypatch):
    monkeypatch.setattr(testing, "TEST_TIMEOUT_SECONDS", 0.05)

    class SlowSender(InMemoryWebhookSender):
        async def send(self, url, secret, event):
            await asyncio.sleep(1)

    app.dependency_overrides[get_test_webhook_sender] = lambda: SlowSender()
    resp = await run_test(world, "webhook_channel")
    assert resp.json()["data"]["detail"] == "Send timed out"


async def test_unreadable_channel_config_is_a_conflict(world):
    app.dependency_overrides[get_channel_cipher] = lambda: ChannelCipher([Fernet.generate_key().decode()])
    resp = await run_test(world, "email_channel")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "channel_config_unreadable"


# --- who may test, and how often ------------------------------------------------------------


async def test_viewer_cannot_test_and_outsider_sees_nothing(world):
    assert (await run_test(world, "email_channel", who="viewer")).status_code == 403
    assert (await run_test(world, "email_channel", who="outsider")).status_code == 404
    assert world["email"].sent == []


async def test_unknown_channel_is_404(world):
    resp = await world["client"].post(
        f"/v1/notification-channels/{uuid4()}/test", headers=bearer(world["editor"])
    )
    assert resp.status_code == 404


async def test_requires_login(world):
    resp = await world["client"].post(f"/v1/notification-channels/{world['email_channel']}/test")
    assert resp.status_code == 401


async def test_rate_limited_per_channel(world):
    for _ in range(testing.MAX_TESTS_PER_WINDOW):
        assert (await run_test(world, "email_channel")).status_code == 200
    limited = await run_test(world, "email_channel")
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "rate_limit_exceeded"
    assert limited.headers["retry-after"] == str(testing.WINDOW_SECONDS)
    assert len(world["email"].sent) == testing.MAX_TESTS_PER_WINDOW
    # Other channels have their own budget
    assert (await run_test(world, "webhook_channel")).status_code == 200


async def test_outsiders_cannot_use_up_the_limit(world):
    for _ in range(testing.MAX_TESTS_PER_WINDOW + 2):
        await run_test(world, "email_channel", who="outsider")
        await run_test(world, "email_channel", who="viewer")
    assert (await run_test(world, "email_channel")).status_code == 200


async def test_redis_outage_is_a_503(world):
    world["redis"].down = True
    resp = await run_test(world, "email_channel")
    assert resp.status_code == 503
    assert world["email"].sent == []


async def test_api_keys_need_the_write_scope(world):
    async def key(scopes):
        resp = await world["client"].post(
            "/v1/api-keys",
            json={"organization_id": world["org_id"], "name": f"k-{secrets.token_hex(3)}", "scopes": scopes},
            headers=bearer(world["owner"]),
        )
        return {"Authorization": f"Bearer {resp.json()['data']['key']}"}

    denied = await run_test(world, "email_channel", headers=await key(["notification_channels:read"]))
    assert denied.status_code == 403
    allowed = await run_test(world, "email_channel", headers=await key(["notification_channels:write"]))
    assert allowed.status_code == 200


# --- Slack ------------------------------------------------------------------------------


async def test_slack_channel_test_is_sent(world):
    resp = await run_test(world, "slack_channel")
    data = resp.json()["data"]
    assert (data["status"], data["provider_message_id"]) == ("sent", "slack-200")
    url, message = world["slack"].sent[0]
    assert url == SLACK_URL
    assert "slack channel" in str(message)


async def test_a_removed_slack_webhook_is_reported_safely(world):
    world["slack"].fail_with = PermanentSlackError("Slack rejected the message: 404 (no_service)")
    data = (await run_test(world, "slack_channel")).json()["data"]
    assert (data["status"], data["detail"]) == ("failed", "Slack rejected the message: 404 (no_service)")
    assert "abcdefSECRET123" not in str(data)
