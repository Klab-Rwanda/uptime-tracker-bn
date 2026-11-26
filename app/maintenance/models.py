from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.util.database import Base


class MaintenanceWindow(Base):
    """Planned work on some monitors, e.g. "Database upgrade, 01:00-03:00".

    While a window is active its monitors show MAINTENANCE on status pages, and
    (with suppress_notifications) incidents during it send no alerts.
    """

    __tablename__ = "maintenance_windows"
    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="maintenance_windows_ends_after_start"),
        Index("maintenance_windows_org_starts_idx", "organization_id", "starts_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    suppress_notifications: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
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

    monitors: Mapped[list["MaintenanceWindowMonitor"]] = relationship(
        back_populates="window",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class MaintenanceWindowMonitor(Base):
    """A monitor covered by a window. Deleting the monitor removes it here."""

    __tablename__ = "maintenance_window_monitors"
    __table_args__ = (Index("maintenance_window_monitors_monitor_idx", "monitor_id"),)

    window_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("maintenance_windows.id", ondelete="CASCADE"),
        primary_key=True,
    )
    monitor_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("monitors.id", ondelete="CASCADE"),
        primary_key=True,
    )

    window: Mapped[MaintenanceWindow] = relationship(back_populates="monitors")
