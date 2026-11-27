"""The whole product in one story, with the real code everywhere except the outside
world (email, webhook and Slack senders are in-memory fakes):

  API setup -> check worker sees the site go down -> incident -> alerts queued ->
  delivery worker sends them -> public status page shows the outage ->
  site recovers -> incident resolved -> "back up" alerts -> page history.
"""

import json
import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.checks.result import CheckResult
from app.main import app
from app.models.monitor import Monitor
from app.notification_channels.crypto import ChannelCipher
from app.notification_channels.dependencies import get_channel_cipher
from app.services.email.fake import InMemoryEmailProvider
from app.services.notifications.channel_sender import ChannelDeliverySender
from app.services.notifications.dispatcher import process_due
from app.services.notifications.sql_store import SqlDeliveryStore
from app.services.slack.fake import InMemorySlackSender
from app.services.webhook.fake import InMemoryWebhookSender
from app.util.config import get_settings
from app.util.database import get_db_session
from app.util.redis import get_redis_client
from app.workers.check_worker import run_due_monitors
from tests.conftest import dispose_test_engine, make_test_engine
from tests.test_status_pages_api import FakeRedis, bearer, create_account, sent_emails

CIPHER = ChannelCipher([Fernet.generate_key().decode()])
SLACK_URL = "https://hooks.slack.com/services/T0001/B0002/abcdefSECRET123"
WEBHOOK_URL = "https://hooks.acme.com/uptime"


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def env(request, tmp_path, monkeypatch) -> AsyncIterator[dict]:
    monkeypatch.setenv("JWT_SECRET", secrets.token_urlsafe(48))
    get_settings.cache_clear()
    sent_emails.clear()

    async def fake_send_email(kind: str, *, recipient: str, parameters: dict[str, str]) -> None:
        sent_emails.append((kind, recipient, parameters))

    monkeypatch.setattr("app.auth.service.send_email", fake_send_email)
    monkeypatch.setattr("app.organizations.service.send_email", fake_send_email)

    engine = await make_test_engine(request.param, tmp_path)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    redis = FakeRedis()
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_redis_client] = lambda: redis
    app.dependency_overrides[get_channel_cipher] = lambda: CIPHER
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield {"client": client, "sessions": sessions, "store": SqlDeliveryStore(engine), "redis": redis}
    app.dependency_overrides.clear()
    await dispose_test_engine(engine)
    get_settings.cache_clear()
    get_redis_client.cache_clear()


class Website:
    """Plays the customer's site: answers each check with the next scripted state."""

    def __init__(self, start: datetime) -> None:
        self.clock = start
        self.state = "up"

    async def __call__(self, monitor: Monitor) -> CheckResult:
        self.clock += timedelta(minutes=1)
        return CheckResult(
            monitor_id=str(monitor.id),
            status=self.state,
            response_time_ms=40,
            checked_at=self.clock,
            error_message=None if self.state == "up" else "Unexpected status code: 503",
        )


async def run_checks(env, monitor_id: str, website: Website, times: int) -> None:
    for _ in range(times):
        async with env["sessions"]() as s:
            (await s.get(Monitor, UUID(monitor_id))).next_check_at = website.clock
            await s.commit()
        await run_due_monitors(
            env["sessions"], check=website, alerts=env["store"], now=website.clock + timedelta(seconds=1)
        )


async def deliver(env, outbox: dict) -> None:
    sender = ChannelDeliverySender(
        env["sessions"],
        cipher=CIPHER,
        email_provider=outbox["email"],
        webhook_sender=outbox["webhook"],
        slack_sender=outbox["slack"],
    )
    await process_due(env["store"], sender, clock=lambda: datetime.now(UTC) + timedelta(days=1))


async def public_page(env, slug: str) -> dict:
    env["redis"].data.clear()  # read fresh, not the 15-second cache
    resp = await env["client"].get(f"/status/{slug}")
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


async def test_an_outage_from_start_to_finish(env):
    client = env["client"]
    tag = uuid4().hex[:6]

    # 1. A customer signs up and sets everything up through the API
    owner = await create_account(client, f"owner_{tag}@example.com")
    headers = bearer(owner)
    org = await client.post("/v1/organizations", json={"name": "Acme", "slug": f"acme-{tag}"}, headers=headers)
    org_id = org.json()["data"]["id"]

    async def channel(kind: str, config: dict) -> str:
        resp = await client.post(
            "/v1/notification-channels",
            json={"organization_id": org_id, "name": f"On-call {kind}", "type": kind, "config": config},
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["data"]["id"]

    channel_ids = [
        await channel("email", {"recipients": ["oncall@acme.com"]}),
        await channel("webhook", {"webhook_url": WEBHOOK_URL}),
        await channel("slack", {"webhook_url": SLACK_URL}),
    ]
    monitor = await client.post(
        "/v1/monitors",
        json={
            "organization_id": org_id,
            "name": "Customer API",
            "type": "http",
            "target": "https://api.acme.com/health",
            "retries": 0,
            "notification_channel_ids": channel_ids,
        },
        headers=headers,
    )
    assert monitor.status_code == 201, monitor.text
    monitor_id = monitor.json()["data"]["id"]
    slug = f"acme-{tag}"
    page = await client.post(
        "/v1/status-pages",
        json={
            "organization_id": org_id,
            "title": "Acme Status",
            "slug": slug,
            "groups": [{"name": "Services", "monitor_ids": [monitor_id]}],
        },
        headers=headers,
    )
    assert page.status_code == 201, page.text

    outbox = {"email": InMemoryEmailProvider(), "webhook": InMemoryWebhookSender(), "slack": InMemorySlackSender()}
    website = Website(start=datetime.now(UTC) - timedelta(hours=1))

    # 2. All good at first
    await run_checks(env, monitor_id, website, times=2)
    data = await public_page(env, slug)
    assert (data["overall_status"], data["active_incidents"]) == ("operational", [])

    # 3. The site goes down: the worker opens an incident and alerts go out everywhere
    website.state = "down"
    await run_checks(env, monitor_id, website, times=3)
    await deliver(env, outbox)

    incidents = (await client.get(f"/v1/incidents?organization_id={org_id}", headers=headers)).json()["data"]
    assert [(i["status"], i["monitor_id"]) for i in incidents] == [("open", monitor_id)]
    assert len(outbox["email"].sent) == 1
    assert outbox["webhook"].sent[0][1].event_type == "incident.opened"
    assert outbox["slack"].sent[0][1]["text"] == "🔴 Customer API is down"

    data = await public_page(env, slug)
    assert data["overall_status"] == "major_outage"
    assert data["groups"][0]["monitors"] == [{"name": "Customer API", "status": "down"}]
    assert [i["title"] for i in data["active_incidents"]] == ["Customer API is down"]

    # 4. More failed checks don't send the alerts again
    await run_checks(env, monitor_id, website, times=2)
    await deliver(env, outbox)
    assert (len(outbox["email"].sent), len(outbox["webhook"].sent), len(outbox["slack"].sent)) == (1, 1, 1)

    # 5. The site recovers: incident resolved, "back up" everywhere, page history
    website.state = "up"
    await run_checks(env, monitor_id, website, times=1)
    await deliver(env, outbox)

    incidents = (await client.get(f"/v1/incidents?organization_id={org_id}", headers=headers)).json()["data"]
    assert incidents[0]["status"] == "resolved"
    assert len(outbox["email"].sent) == 2
    assert outbox["webhook"].sent[1][1].event_type == "incident.resolved"
    assert outbox["slack"].sent[1][1]["text"] == "🟢 Customer API is back up"

    data = await public_page(env, slug)
    assert (data["overall_status"], data["active_incidents"]) == ("operational", [])
    [past] = data["incident_history"]
    assert past["title"] == "Customer API is down"
    assert past["duration_seconds"] > 0

    # 6. Nothing private ever reached the public page
    assert "api.acme.com" not in json.dumps(data) and "503" not in json.dumps(data)
