from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.util.database import Base


class Monitor(Base):
    __tablename__ = "monitors"
    __table_args__ = (
        Index("monitors_org_project_idx", "organization_id", "project_id"),
        Index("monitors_next_check_idx", "is_active", "next_check_at"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )

    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )

    project_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    monitor_type: Mapped[str] = mapped_column(String(10), nullable=False)
    target: Mapped[str] = mapped_column(Text, nullable=False)

    method: Mapped[str | None] = mapped_column(String(10), nullable=True, default="GET")
    expected_status_codes: Mapped[str | None] = mapped_column(String(255), nullable=True)
    keyword: Mapped[str | None] = mapped_column(Text, nullable=True)

    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    retry_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    check_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60)

    regions: Mapped[str | None] = mapped_column(Text, nullable=True)
    notification_channel_ids: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    current_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")

    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    consecutive_successes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_check_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
