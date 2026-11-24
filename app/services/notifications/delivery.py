import asyncio
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol


class DeliveryStatus(StrEnum):
    PENDING = "pending"  # waiting for its first or next attempt
    IN_PROGRESS = "in_progress"  # claimed by a worker (has a lease)
    SENT = "sent"
    FAILED = "failed"


@dataclass
class DeliveryRecord:
    id: str
    idempotency_key: str
    channel_id: str
    payload: dict[str, Any]
    status: DeliveryStatus
    attempts: int
    next_attempt_at: datetime
    created_at: datetime
    updated_at: datetime
    lease_until: datetime | None = None
    last_error: str | None = None
    provider_message_id: str | None = None
    sent_at: datetime | None = None


def due_time(record: DeliveryRecord) -> datetime | None:
    """When the record may next be claimed, or None if it never will be."""
    if record.status is DeliveryStatus.PENDING:
        return record.next_attempt_at
    if record.status is DeliveryStatus.IN_PROGRESS:
        return record.lease_until  # a claim whose lease expired is up for grabs again
    return None


def is_claimable(record: DeliveryRecord, now: datetime) -> bool:
    due = due_time(record)
    return due is not None and due <= now


class DeliveryStore(Protocol):
    """What the dispatcher needs. InMemoryDeliveryStore and SqlDeliveryStore both
    implement it and pass the same tests.

    The mark_* and schedule_retry methods take the attempt number the worker
    got from claim() and return False if it no longer matches. That rejects a
    slow worker whose lease expired and whose delivery was claimed by another.
    """

    async def create_if_absent(
        self, *, idempotency_key: str, channel_id: str, payload: dict[str, Any], now: datetime
    ) -> tuple[DeliveryRecord, bool]: ...

    async def claim(
        self, delivery_id: str, *, now: datetime, lease_seconds: float
    ) -> DeliveryRecord | None: ...

    async def mark_sent(
        self, delivery_id: str, *, attempt: int, now: datetime, provider_message_id: str | None
    ) -> bool: ...

    async def schedule_retry(
        self, delivery_id: str, *, attempt: int, now: datetime, next_attempt_at: datetime, error: str
    ) -> bool: ...

    async def mark_failed(
        self, delivery_id: str, *, attempt: int, now: datetime, error: str
    ) -> bool: ...

    async def get(self, delivery_id: str) -> DeliveryRecord | None: ...

    async def list_due(self, *, now: datetime, limit: int = 50) -> list[DeliveryRecord]: ...


class InMemoryDeliveryStore:
    """For tests and local runs. A lock makes each operation atomic, like one SQL statement."""

    def __init__(self) -> None:
        self._records: dict[str, DeliveryRecord] = {}
        self._by_key: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def create_if_absent(
        self, *, idempotency_key: str, channel_id: str, payload: dict[str, Any], now: datetime
    ) -> tuple[DeliveryRecord, bool]:
        async with self._lock:
            existing_id = self._by_key.get(idempotency_key)
            if existing_id is not None:
                return replace(self._records[existing_id]), False
            record = DeliveryRecord(
                id=str(uuid.uuid4()),
                idempotency_key=idempotency_key,
                channel_id=channel_id,
                payload=dict(payload),
                status=DeliveryStatus.PENDING,
                attempts=0,
                next_attempt_at=now,
                created_at=now,
                updated_at=now,
            )
            self._records[record.id] = record
            self._by_key[idempotency_key] = record.id
            return replace(record), True

    async def claim(
        self, delivery_id: str, *, now: datetime, lease_seconds: float
    ) -> DeliveryRecord | None:
        async with self._lock:
            record = self._records.get(delivery_id)
            if record is None or not is_claimable(record, now):
                return None
            record.status = DeliveryStatus.IN_PROGRESS
            record.attempts += 1
            record.lease_until = now + timedelta(seconds=lease_seconds)
            record.updated_at = now
            return replace(record)

    def _active(self, delivery_id: str, attempt: int) -> DeliveryRecord | None:
        record = self._records.get(delivery_id)
        if record is None:
            return None
        if record.status is not DeliveryStatus.IN_PROGRESS or record.attempts != attempt:
            return None
        return record

    async def mark_sent(
        self, delivery_id: str, *, attempt: int, now: datetime, provider_message_id: str | None
    ) -> bool:
        async with self._lock:
            record = self._active(delivery_id, attempt)
            if record is None:
                return False
            record.status = DeliveryStatus.SENT
            record.sent_at = now
            record.provider_message_id = provider_message_id
            record.last_error = None
            record.lease_until = None
            record.updated_at = now
            return True

    async def schedule_retry(
        self, delivery_id: str, *, attempt: int, now: datetime, next_attempt_at: datetime, error: str
    ) -> bool:
        async with self._lock:
            record = self._active(delivery_id, attempt)
            if record is None:
                return False
            record.status = DeliveryStatus.PENDING
            record.next_attempt_at = next_attempt_at
            record.last_error = error
            record.lease_until = None
            record.updated_at = now
            return True

    async def mark_failed(
        self, delivery_id: str, *, attempt: int, now: datetime, error: str
    ) -> bool:
        async with self._lock:
            record = self._active(delivery_id, attempt)
            if record is None:
                return False
            record.status = DeliveryStatus.FAILED
            record.last_error = error
            record.lease_until = None
            record.updated_at = now
            return True

    async def get(self, delivery_id: str) -> DeliveryRecord | None:
        async with self._lock:
            record = self._records.get(delivery_id)
            return replace(record) if record is not None else None

    async def list_due(self, *, now: datetime, limit: int = 50) -> list[DeliveryRecord]:
        async with self._lock:
            candidates: list[tuple[datetime, DeliveryRecord]] = []
            for record in self._records.values():
                when = due_time(record)
                if when is not None and when <= now:
                    candidates.append((when, record))
            candidates.sort(key=lambda pair: pair[0])
            return [replace(record) for _, record in candidates[:limit]]
