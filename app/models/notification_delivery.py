from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.util.database import Base


class DeliveryStatus(StrEnum):
    SENDING = "sending"  # claimed by a worker until locked_until
    PENDING = "pending"  # waiting for another try at next_attempt_at
    SENT = "sent"  # done, never send again
    FAILED = "failed"  # permanent failure, never retry


class NotificationDelivery(Base):
    """One row per notification, keyed by its idempotency key.

    The unique key is what stops the same alert being sent twice, even when
    two workers handle the same incident at the same moment.
    """

    __tablename__ = "notification_deliveries"
    __table_args__ = (
        CheckConstraint(
            "status IN ('sending', 'pending', 'sent', 'failed')",
            name="notification_deliveries_status_valid",
        ),
        CheckConstraint("attempts >= 0", name="notification_deliveries_attempts_valid"),
        CheckConstraint(
            "status <> 'sending' OR locked_until IS NOT NULL",
            name="notification_deliveries_sending_has_lease",
        ),
        CheckConstraint(
            "(status = 'sent') = (sent_at IS NOT NULL)",
            name="notification_deliveries_sent_at_matches_status",
        ),
        # For the worker that picks up re-queued deliveries
        Index("notification_deliveries_due_idx", "status", "next_attempt_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    channel_type: Mapped[str] = mapped_column(String(20), nullable=False)  # "email", "webhook"
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    # Delivery runs (claims), not the quick retries inside one run
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Safe, short error message only (never a traceback, which may hold URL tokens)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
