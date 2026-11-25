from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

# The shared Base: the heartbeats table must live in the same metadata as
# monitors, or its foreign key can't be resolved and tests can't create it
from app.util.database import Base


class Heartbeat(Base):
    """One stored result of one check (after the check's own retries)."""

    __tablename__ = "heartbeats"
    __table_args__ = (
        CheckConstraint(
            "status IN ('up', 'down', 'pending')", name="heartbeats_status_valid"
        ),
        # "The latest N heartbeats of a monitor": incident detection, dashboards
        Index("heartbeats_monitor_checked_idx", "monitor_id", "checked_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    monitor_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("monitors.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    response_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
