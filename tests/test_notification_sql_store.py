from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError

from app.models.notification_delivery import NotificationDelivery
from app.services.notifications.sql_store import SqlDeliveryStore

# `delivery_engine` (tests/conftest.py) is SQLite, plus PostgreSQL when available.

NOW = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)


async def test_payload_round_trips(delivery_engine):
    store = SqlDeliveryStore(delivery_engine)
    payload = {
        "incident_id": "inc_1",
        "event": "opened",
        "recipients": ["a@b.com", "c@d.com"],
        "data": {"monitor_name": "Customer API", "attempts": 2, "ok": False, "note": None},
    }
    record, _ = await store.create_if_absent(
        idempotency_key="k", channel_id="chan_1", payload=payload, now=NOW
    )
    assert (await store.get(record.id)).payload == payload


async def test_timestamps_come_back_timezone_aware(delivery_engine):
    store = SqlDeliveryStore(delivery_engine)
    record, _ = await store.create_if_absent(
        idempotency_key="k", channel_id="c", payload={}, now=NOW
    )
    claimed = await store.claim(record.id, now=NOW, lease_seconds=60)
    assert claimed.lease_until == NOW + timedelta(seconds=60)
    for value in (claimed.created_at, claimed.updated_at, claimed.next_attempt_at):
        assert value.tzinfo is not None


async def test_bad_ids_are_ignored_by_every_method(delivery_engine):
    store = SqlDeliveryStore(delivery_engine)
    for bad in ("not-a-uuid", str(uuid4())):
        assert await store.get(bad) is None
        assert await store.claim(bad, now=NOW, lease_seconds=60) is None
        assert await store.mark_sent(bad, attempt=1, now=NOW, provider_message_id=None) is False
        assert await store.mark_failed(bad, attempt=1, now=NOW, error="x") is False
        assert (
            await store.schedule_retry(bad, attempt=1, now=NOW, next_attempt_at=NOW, error="x")
            is False
        )


def _row(**overrides):
    values = {
        "id": uuid4(),
        "idempotency_key": str(uuid4()),
        "channel_id": "c",
        "payload": {},
        "status": "pending",
        "attempts": 0,
        "next_attempt_at": NOW,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return values


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "exploded"},
        {"status": "in_progress"},  # in progress without a lease
        {"status": "sent"},  # sent without sent_at
        {"sent_at": NOW},  # sent_at on a pending delivery
        {"attempts": -1},
    ],
)
async def test_database_rejects_impossible_rows(delivery_engine, overrides):
    with pytest.raises(IntegrityError):
        async with delivery_engine.begin() as conn:
            await conn.execute(insert(NotificationDelivery.__table__).values(**_row(**overrides)))


async def test_database_rejects_duplicate_keys(delivery_engine):
    with pytest.raises(IntegrityError):
        async with delivery_engine.begin() as conn:
            table = NotificationDelivery.__table__
            await conn.execute(insert(table).values(**_row(idempotency_key="same")))
            await conn.execute(insert(table).values(**_row(idempotency_key="same")))
