from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import CursorResult, Row, Table, and_, case, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from app.models.notification_delivery import NotificationDelivery
from app.services.notifications.delivery import DeliveryRecord, DeliveryStatus

_table = cast(Table, NotificationDelivery.__table__)
_c = _table.c


def _parse_id(delivery_id: str) -> UUID | None:
    try:
        return UUID(delivery_id)
    except ValueError:
        return None


def _aware(value: datetime | None) -> datetime | None:
    # SQLite returns timestamps without a timezone; everything we store is UTC
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _to_record(row: Row[Any]) -> DeliveryRecord:
    m = row._mapping
    next_attempt_at = _aware(m["next_attempt_at"])
    created_at = _aware(m["created_at"])
    updated_at = _aware(m["updated_at"])
    assert next_attempt_at and created_at and updated_at  # NOT NULL columns
    return DeliveryRecord(
        id=str(m["id"]),
        idempotency_key=m["idempotency_key"],
        channel_id=m["channel_id"],
        payload=dict(m["payload"]),
        status=DeliveryStatus(m["status"]),
        attempts=m["attempts"],
        next_attempt_at=next_attempt_at,
        created_at=created_at,
        updated_at=updated_at,
        lease_until=_aware(m["lease_until"]),
        last_error=m["last_error"],
        provider_message_id=m["provider_message_id"],
        sent_at=_aware(m["sent_at"]),
    )


def _claimable(now: datetime) -> Any:
    return or_(
        and_(_c.status == DeliveryStatus.PENDING.value, _c.next_attempt_at <= now),
        and_(_c.status == DeliveryStatus.IN_PROGRESS.value, _c.lease_until <= now),
    )


class SqlDeliveryStore:
    """DeliveryStore on the notification_deliveries table.

    Every method is a single statement in its own transaction, so it is atomic
    across workers and processes (PostgreSQL in production, SQLite in tests).
    """

    def __init__(self, engine: AsyncEngine) -> None:
        if engine.dialect.name not in ("postgresql", "sqlite"):
            raise ValueError(f"Unsupported database: {engine.dialect.name}")
        self._engine = engine
        self._insert = pg_insert if engine.dialect.name == "postgresql" else sqlite_insert

    async def create_if_absent(
        self, *, idempotency_key: str, channel_id: str, payload: dict[str, Any], now: datetime
    ) -> tuple[DeliveryRecord, bool]:
        stmt = (
            self._insert(_table)
            .values(
                id=uuid4(),
                idempotency_key=idempotency_key,
                channel_id=channel_id,
                payload=dict(payload),
                status=DeliveryStatus.PENDING.value,
                attempts=0,
                next_attempt_at=now,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=["idempotency_key"])
        )
        async with self._engine.begin() as conn:
            result = cast(CursorResult[Any], await conn.execute(stmt))
            row = (
                await conn.execute(select(_table).where(_c.idempotency_key == idempotency_key))
            ).one()
        return _to_record(row), result.rowcount == 1

    async def claim(
        self, delivery_id: str, *, now: datetime, lease_seconds: float
    ) -> DeliveryRecord | None:
        uid = _parse_id(delivery_id)
        if uid is None:
            return None
        stmt = (
            update(_table)
            .where(_c.id == uid, _claimable(now))
            .values(
                status=DeliveryStatus.IN_PROGRESS.value,
                attempts=_c.attempts + 1,
                lease_until=now + timedelta(seconds=lease_seconds),
                updated_at=now,
            )
            .returning(*_c)
        )
        async with self._engine.begin() as conn:
            row = (await conn.execute(stmt)).first()
        return _to_record(row) if row is not None else None

    async def _finish(self, delivery_id: str, attempt: int, **values: Any) -> bool:
        """Apply a result only if this worker's claim is still the current one."""
        uid = _parse_id(delivery_id)
        if uid is None:
            return False
        stmt = (
            update(_table)
            .where(
                _c.id == uid,
                _c.status == DeliveryStatus.IN_PROGRESS.value,
                _c.attempts == attempt,
            )
            .values(lease_until=None, **values)
        )
        async with self._engine.begin() as conn:
            result = cast(CursorResult[Any], await conn.execute(stmt))
        return result.rowcount == 1

    async def mark_sent(
        self, delivery_id: str, *, attempt: int, now: datetime, provider_message_id: str | None
    ) -> bool:
        return await self._finish(
            delivery_id,
            attempt,
            status=DeliveryStatus.SENT.value,
            sent_at=now,
            provider_message_id=provider_message_id,
            last_error=None,
            updated_at=now,
        )

    async def schedule_retry(
        self, delivery_id: str, *, attempt: int, now: datetime, next_attempt_at: datetime, error: str
    ) -> bool:
        return await self._finish(
            delivery_id,
            attempt,
            status=DeliveryStatus.PENDING.value,
            next_attempt_at=next_attempt_at,
            last_error=error,
            updated_at=now,
        )

    async def mark_failed(
        self, delivery_id: str, *, attempt: int, now: datetime, error: str
    ) -> bool:
        return await self._finish(
            delivery_id,
            attempt,
            status=DeliveryStatus.FAILED.value,
            last_error=error,
            updated_at=now,
        )

    async def get(self, delivery_id: str) -> DeliveryRecord | None:
        uid = _parse_id(delivery_id)
        if uid is None:
            return None
        async with self._engine.connect() as conn:
            row = (await conn.execute(select(_table).where(_c.id == uid))).first()
        return _to_record(row) if row is not None else None

    async def list_due(self, *, now: datetime, limit: int = 50) -> list[DeliveryRecord]:
        due_at = case(
            (_c.status == DeliveryStatus.PENDING.value, _c.next_attempt_at),
            else_=_c.lease_until,
        )
        stmt = select(_table).where(_claimable(now)).order_by(due_at, _c.id).limit(limit)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [_to_record(row) for row in rows]
