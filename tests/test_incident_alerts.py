import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.checks.result import CheckResult
from app.incidents.alerts import (
    AlertEvent,
    alert_key,
    enqueue_incident_alerts,
    ensure_incident_alerts,
)
from app.incidents.service import Evaluation, open_incident, resolve_incident
from app.models.monitor import Monitor
from app.notification_channels.crypto import ChannelCipher
from app.notification_channels.models import NotificationChannel
from app.organizations.models import Organization
from app.services.email.base import RetryableEmailError
from app.services.email.fake import InMemoryEmailProvider
from app.services.incident_rules import IncidentAction
from app.services.notifications.channel_sender import ChannelDeliverySender
from app.services.notifications.dispatcher import AttemptOutcome, process_due
from app.services.notifications.retry import PermanentDeliveryError
from app.services.notifications.sql_store import SqlDeliveryStore
from app.services.webhook.fake import InMemoryWebhookSender
from app.workers.check_worker import run_due_monitors
from tests.conftest import dispose_test_engine, make_test_engine

T0 = datetime(2026, 11, 26, 9, 0, tzinfo=UTC)
CIPHER = ChannelCipher([Fernet.generate_key().decode()])
WEBHOOK_URL = "https://hooks.acme.com/services/T0/secret-token-123"
SIGNING_SECRET = "whsec_signing_secret_value"


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def engine(request, tmp_path) -> AsyncIterator[AsyncEngine]:
    engine = await make_test_engine(request.param, tmp_path)
    yield engine
    await dispose_test_engine(engine)


@pytest.fixture
def sessions(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def store(engine) -> SqlDeliveryStore:
    return SqlDeliveryStore(engine)


async def add_channel(s: AsyncSession, org_id: UUID, kind: str, name: str) -> UUID:
    config = (
        {"recipients": ["oncall@acme.com", "ops@acme.com"]}
        if kind == "email"
        else {"webhook_url": WEBHOOK_URL, "secret": SIGNING_SECRET}
    )
    channel = NotificationChannel(
        organization_id=org_id, name=name, channel_type=kind, config_encrypted=CIPHER.encrypt(config)
    )
    s.add(channel)
    await s.flush()
    return channel.id


@pytest_asyncio.fixture
async def world(sessions) -> dict:
    """Org A: a monitor using an email + a webhook channel, plus one foreign and
    one missing channel ID. Org B: its own channel."""
    async with sessions() as s:
        org_a = Organization(name="Acme", slug=f"acme-{uuid4().hex[:6]}")
        org_b = Organization(name="Other", slug=f"other-{uuid4().hex[:6]}")
        s.add_all([org_a, org_b])
        await s.flush()
        email_id = await add_channel(s, org_a.id, "email", "On-call email")
        webhook_id = await add_channel(s, org_a.id, "webhook", "On-call hook")
        foreign_id = await add_channel(s, org_b.id, "webhook", "Other tenant hook")
        monitor = Monitor(
            organization_id=org_a.id,
            name="Customer API",
            monitor_type="http",
            target="https://status.acme.com/health?token=SECRET123",
            check_interval_seconds=60,
            retry_count=0,
            next_check_at=T0,
            notification_channel_ids=json.dumps(
                [str(email_id), str(webhook_id), str(foreign_id), str(uuid4())]
            ),
        )
        s.add(monitor)
        await s.commit()
        return {
            "org_a": org_a.id,
            "org_b": org_b.id,
            "monitor": monitor.id,
            "email": email_id,
            "webhook": webhook_id,
            "foreign": foreign_id,
        }


async def open_now(sessions, world):
    async with sessions() as s:
        monitor = await s.get(Monitor, world["monitor"])
        incident, _ = await open_incident(s, monitor, started_at=T0, cause="HTTP 503 from API", now=T0)
        return incident


async def queued(store) -> list:
    return await store.list_due(now=T0 + timedelta(days=1), limit=100)


# --- queueing alerts ---------------------------------------------------------------------


async def test_one_alert_per_channel_of_the_same_organization(sessions, store, world):
    incident = await open_now(sessions, world)
    async with sessions() as s:
        created = await enqueue_incident_alerts(s, store, incident, AlertEvent.OPENED, now=T0)

    records = await queued(store)
    assert created == 2
    assert {r.channel_id for r in records} == {str(world["email"]), str(world["webhook"])}
    assert str(world["foreign"]) not in {r.channel_id for r in records}  # other tenant: never
    assert {r.idempotency_key for r in records} == {
        alert_key(incident.id, AlertEvent.OPENED, world["email"]),
        alert_key(incident.id, AlertEvent.OPENED, world["webhook"]),
    }


async def test_queueing_twice_creates_nothing_new(sessions, store, world):
    incident = await open_now(sessions, world)
    async with sessions() as s:
        await enqueue_incident_alerts(s, store, incident, AlertEvent.OPENED, now=T0)
        again = await enqueue_incident_alerts(s, store, incident, AlertEvent.OPENED, now=T0)
    assert again == 0
    assert len(await queued(store)) == 2


async def test_payload_describes_the_incident_without_secrets(sessions, store, world):
    incident = await open_now(sessions, world)
    async with sessions() as s:
        await enqueue_incident_alerts(s, store, incident, AlertEvent.OPENED, now=T0)
    record = (await queued(store))[0]

    assert record.payload["event"] == "opened"
    assert record.payload["monitor_name"] == "Customer API"
    assert record.payload["cause"] == "HTTP 503 from API"
    assert record.payload["started_at"] == "2026-11-26T09:00:00Z"
    assert record.payload["organization_id"] == str(world["org_a"])
    stored = json.dumps(record.payload)
    for secret in ("SECRET123", "secret-token-123", SIGNING_SECRET, "oncall@acme.com"):
        assert secret not in stored
    assert record.payload["target"] == "https://status.acme.com/health"


async def test_monitor_without_channels_queues_nothing(sessions, store, world):
    async with sessions() as s:
        monitor = await s.get(Monitor, world["monitor"])
        monitor.notification_channel_ids = None
        await s.commit()
    incident = await open_now(sessions, world)
    async with sessions() as s:
        assert await enqueue_incident_alerts(s, store, incident, AlertEvent.OPENED) == 0


async def test_ensure_alerts_matches_the_evaluation(sessions, store, world):
    incident = await open_now(sessions, world)
    async with sessions() as s:
        assert await ensure_incident_alerts(s, store, Evaluation(IncidentAction.NONE, "no incident")) == 0
        opened = await ensure_incident_alerts(s, store, Evaluation(IncidentAction.NONE, "still down", incident))
        resolved_incident = await resolve_incident(s, incident.id, resolved_at=T0 + timedelta(minutes=5))
        resolved = await ensure_incident_alerts(
            s, store, Evaluation(IncidentAction.RESOLVE, "recovered", resolved_incident)
        )
    assert (opened, resolved) == (2, 2)
    events = sorted(r.payload["event"] for r in await queued(store))
    assert events == ["opened", "opened", "resolved", "resolved"]


# --- the check worker queues alerts automatically ------------------------------------------


class ScriptedChecks:
    def __init__(self, *states: str) -> None:
        self.states = list(states)
        self.clock = T0

    async def __call__(self, monitor: Monitor) -> CheckResult:
        self.clock += timedelta(minutes=1)
        state = self.states.pop(0)
        return CheckResult(
            monitor_id=str(monitor.id),
            status=state,
            response_time_ms=5,
            checked_at=self.clock,
            error_message=None if state == "up" else "Unexpected status code: 503",
        )


async def run_check(sessions, store, world, checks):
    async with sessions() as s:
        (await s.get(Monitor, world["monitor"])).next_check_at = T0
        await s.commit()
    await run_due_monitors(sessions, check=checks, alerts=store, now=T0 + timedelta(seconds=1))


async def test_worker_queues_opened_then_resolved_alerts(sessions, store, world):
    checks = ScriptedChecks("down", "down", "down", "up")
    await run_check(sessions, store, world, checks)
    assert await queued(store) == []  # one failure: no incident, no alert

    await run_check(sessions, store, world, checks)
    assert sorted(r.payload["event"] for r in await queued(store)) == ["opened", "opened"]

    await run_check(sessions, store, world, checks)  # still down: no duplicates
    assert len(await queued(store)) == 2

    await run_check(sessions, store, world, checks)
    events = sorted(r.payload["event"] for r in await queued(store))
    assert events == ["opened", "opened", "resolved", "resolved"]


# --- sending ---------------------------------------------------------------------------------


@pytest.fixture
def fakes():
    return {"email": InMemoryEmailProvider(), "webhook": InMemoryWebhookSender()}


def sender_for(sessions, fakes, cipher=CIPHER) -> ChannelDeliverySender:
    return ChannelDeliverySender(
        sessions, cipher=cipher, email_provider=fakes["email"], webhook_sender=fakes["webhook"]
    )


async def queue_opened(sessions, store, world):
    incident = await open_now(sessions, world)
    async with sessions() as s:
        await enqueue_incident_alerts(s, store, incident, AlertEvent.OPENED, now=T0)
    return incident


async def test_full_chain_sends_email_and_webhook(sessions, store, world, fakes):
    incident = await queue_opened(sessions, store, world)
    counts = await process_due(store, sender_for(sessions, fakes), clock=lambda: T0)

    assert counts == {AttemptOutcome.SENT: 2}
    mail = fakes["email"].sent[0]
    assert mail.subject == "[DOWN] Customer API is down"
    assert mail.to == ["oncall@acme.com", "ops@acme.com"]
    assert mail.idempotency_key == alert_key(incident.id, AlertEvent.OPENED, world["email"])
    assert "HTTP 503 from API" in mail.text

    url, event = fakes["webhook"].sent[0]
    assert url == WEBHOOK_URL
    assert event.event_type == "incident.opened"
    assert event.delivery_id == alert_key(incident.id, AlertEvent.OPENED, world["webhook"])
    assert event.data["incident_id"] == str(incident.id)
    assert "organization_id" not in event.data


async def test_resolved_alert_uses_the_resolved_email(sessions, store, world, fakes):
    incident = await open_now(sessions, world)
    async with sessions() as s:
        resolved = await resolve_incident(s, incident.id, resolved_at=T0 + timedelta(minutes=65))
        await enqueue_incident_alerts(s, store, resolved, AlertEvent.RESOLVED, now=T0)
    await process_due(store, sender_for(sessions, fakes), clock=lambda: T0)
    mail = fakes["email"].sent[0]
    assert mail.subject == "[RESOLVED] Customer API is back up"
    assert "1h 5m" in mail.text
    assert fakes["webhook"].sent[0][1].event_type == "incident.resolved"


async def test_deleted_channel_fails_permanently(sessions, store, world, fakes):
    await queue_opened(sessions, store, world)
    async with sessions() as s:
        await s.execute(delete(NotificationChannel).where(NotificationChannel.id == world["webhook"]))
        await s.commit()
    counts = await process_due(store, sender_for(sessions, fakes), clock=lambda: T0)

    assert counts == {AttemptOutcome.SENT: 1, AttemptOutcome.FAILED: 1}
    failed = [r for r in await store.list_due(now=T0 + timedelta(days=9))]
    assert failed == []  # nothing left to retry
    assert fakes["webhook"].sent == []


async def test_channel_of_another_organization_is_refused(sessions, store, world, fakes):
    incident = await open_now(sessions, world)
    record, _ = await store.create_if_absent(
        idempotency_key=f"{incident.id}:opened:{world['foreign']}",
        channel_id=str(world["foreign"]),
        payload={"event": "opened", "organization_id": str(world["org_a"]), "started_at": "2026-11-26T09:00:00Z"},
        now=T0,
    )
    with pytest.raises(PermanentDeliveryError, match="another organization"):
        await sender_for(sessions, fakes)(record)


async def test_unreadable_channel_config_fails_permanently(sessions, store, world, fakes):
    await queue_opened(sessions, store, world)
    wrong_key = ChannelCipher([Fernet.generate_key().decode()])
    counts = await process_due(store, sender_for(sessions, fakes, cipher=wrong_key), clock=lambda: T0)
    assert counts == {AttemptOutcome.FAILED: 2}


async def test_email_outage_is_retried_later(sessions, store, world, fakes):
    await queue_opened(sessions, store, world)
    fakes["email"].fail_with = RetryableEmailError("Brevo returned 503")
    counts = await process_due(store, sender_for(sessions, fakes), clock=lambda: T0)
    assert counts == {AttemptOutcome.SENT: 1, AttemptOutcome.RETRY_SCHEDULED: 1}

    fakes["email"].fail_with = None
    later = T0 + timedelta(minutes=10)
    assert await process_due(store, sender_for(sessions, fakes), clock=lambda: later) == {
        AttemptOutcome.SENT: 1
    }
    records = await store.list_due(now=later + timedelta(days=1))
    assert records == []
    assert len(fakes["email"].sent) == 1


async def test_missing_email_provider_is_permanent(sessions, store, world):
    await queue_opened(sessions, store, world)
    sender = ChannelDeliverySender(sessions, cipher=CIPHER, webhook_sender=InMemoryWebhookSender())
    counts = await process_due(store, sender, clock=lambda: T0)
    assert counts == {AttemptOutcome.SENT: 1, AttemptOutcome.FAILED: 1}


async def test_alerts_are_sent_only_once(sessions, store, world, fakes):
    await queue_opened(sessions, store, world)
    await process_due(store, sender_for(sessions, fakes), clock=lambda: T0)
    await process_due(store, sender_for(sessions, fakes), clock=lambda: T0 + timedelta(hours=1))
    assert len(fakes["email"].sent) == 1
    assert len(fakes["webhook"].sent) == 1
    assert await store.list_due(now=T0 + timedelta(days=1)) == []  # nothing left to send
