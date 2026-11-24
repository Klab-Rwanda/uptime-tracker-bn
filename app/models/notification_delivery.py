from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.services.notifications.delivery import DeliveryStatus
from app.util.database import Base

_STATUSES = ", ".join(f"'{status}'" for status in DeliveryStatus)


class NotificationDelivery(Base):
    """One row per notification, keyed by its idempotency key.

    The unique key stops the same alert being created twice; the conditional
    UPDATEs in SqlDeliveryStore stop two workers sending it at the same time.
    """

    __tablename__ = "notification_deliveries"
    __table_args__ = (
        CheckConstraint(f"status IN ({_STATUSES})", name="notification_deliveries_status_valid"),
        CheckConstraint("attempts >= 0", name="notification_deliveries_attempts_valid"),
        CheckConstraint(
            "status <> 'in_progress' OR lease_until IS NOT NULL",
            name="notification_deliveries_in_progress_has_lease",
        ),
        CheckConstraint(
            "(status = 'sent') = (sent_at IS NOT NULL)",
            name="notification_deliveries_sent_at_matches_status",
        ),
        # list_due: pending rows that are due, and in-progress rows whose lease expired
        Index("notification_deliveries_due_idx", "status", "next_attempt_at"),
        Index("notification_deliveries_lease_idx", "status", "lease_until"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    # TODO: ForeignKey to notification_channels.id once that table exists (C-17)
    channel_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # What to send, so any worker can (re)send it later
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Safe, short error message only (see classify_failure)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set by the store from its clock, so tests with a fake clock stay consistent
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
