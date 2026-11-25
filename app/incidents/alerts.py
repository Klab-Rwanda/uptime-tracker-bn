"""Turning an incident into one alert per notification channel.

Each alert is a delivery with the key "<incident>:<event>:<channel>", so
creating them again is harmless: the store keeps exactly one per key. That lets
the check worker call ensure_incident_alerts() after every check, which also
heals a run where creating the alerts failed.

The stored payload holds facts about the incident only. Channel secrets (webhook
URL and signing secret, email addresses) stay encrypted in the channel and are
read when the alert is sent.
"""

import json
import logging
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.incidents.service import Evaluation
from app.models.incident import Incident
from app.models.monitor import Monitor
from app.notification_channels.models import NotificationChannel
from app.services.email.templates import safe_target
from app.services.incident_rules import IncidentAction
from app.services.notifications.delivery import DeliveryStore

logger = logging.getLogger(__name__)


class AlertEvent(StrEnum):
    OPENED = "opened"
    RESOLVED = "resolved"


def alert_key(incident_id: UUID, event: AlertEvent, channel_id: UUID) -> str:
    return f"{incident_id}:{event}:{channel_id}"


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:  # SQLite returns naive UTC timestamps
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def build_payload(incident: Incident, monitor: Monitor, event: AlertEvent) -> dict[str, object]:
    """What every channel needs to describe the incident. No secrets in here."""
    return {
        "event": event.value,
        "organization_id": str(incident.organization_id),
        "incident_id": str(incident.id),
        "monitor_id": str(monitor.id),
        "monitor_name": monitor.name,
        # Without credentials or query string: targets can carry tokens
        "target": safe_target(monitor.target),
        "title": incident.title,
        "cause": incident.cause,
        "status": incident.status,
        "started_at": _iso(incident.started_at),
        "resolved_at": _iso(incident.resolved_at),
    }


def _channel_ids(monitor: Monitor) -> list[UUID]:
    try:
        raw = json.loads(monitor.notification_channel_ids or "[]")
    except ValueError:
        return []
    ids: list[UUID] = []
    for value in raw if isinstance(raw, list) else []:
        try:
            parsed = UUID(str(value))
        except ValueError:
            continue
        if parsed not in ids:
            ids.append(parsed)
    return ids


async def enqueue_incident_alerts(
    session: AsyncSession,
    store: DeliveryStore,
    incident: Incident,
    event: AlertEvent,
    *,
    now: datetime | None = None,
) -> int:
    """Create one delivery per channel of the incident's monitor.

    Returns how many deliveries were newly created (0 when they already exist).
    Channels that were deleted or belong to another organization are skipped:
    alerts must never leave the incident's own tenant.
    """
    monitor = await session.get(Monitor, incident.monitor_id)
    if monitor is None:
        return 0
    wanted = _channel_ids(monitor)
    if not wanted:
        return 0

    channels = (
        await session.scalars(
            select(NotificationChannel).where(
                NotificationChannel.organization_id == incident.organization_id,
                NotificationChannel.id.in_(wanted),
            )
        )
    ).all()
    if len(channels) != len(wanted):
        logger.warning(
            "some notification channels of a monitor are missing or foreign",
            extra={"monitor_id": str(monitor.id), "missing": len(wanted) - len(channels)},
        )

    payload = build_payload(incident, monitor, event)
    now = now or datetime.now(UTC)
    created = 0
    for channel in channels:
        _, is_new = await store.create_if_absent(
            idempotency_key=alert_key(incident.id, event, channel.id),
            channel_id=str(channel.id),
            payload={**payload, "channel_type": channel.channel_type},
            now=now,
        )
        created += int(is_new)
    if created:
        logger.info(
            "incident alerts queued",
            extra={"incident_id": str(incident.id), "event": event.value, "count": created},
        )
    return created


async def ensure_incident_alerts(
    session: AsyncSession,
    store: DeliveryStore,
    evaluation: Evaluation,
    *,
    now: datetime | None = None,
) -> int:
    """Make sure the alerts matching an evaluation exist.

    - just resolved      -> "resolved" alerts
    - open/acknowledged  -> "opened" alerts (already there after the first time,
                            so this is a cheap no-op that heals a failed attempt)
    """
    incident = evaluation.incident
    if incident is None:
        return 0
    if evaluation.action is IncidentAction.RESOLVE:
        return await enqueue_incident_alerts(session, store, incident, AlertEvent.RESOLVED, now=now)
    if incident.status != "resolved":
        return await enqueue_incident_alerts(session, store, incident, AlertEvent.OPENED, now=now)
    return 0
