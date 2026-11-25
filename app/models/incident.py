from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.services.incident_logic import IncidentEventType, IncidentStatus
from app.util.database import Base


def _sql_in(values: type[IncidentStatus] | type[IncidentEventType]) -> str:
    return ", ".join(f"'{value}'" for value in values)


class Incident(Base):
    __tablename__ = "incidents"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({_sql_in(IncidentStatus)})", name="incidents_status_valid"
        ),
        # resolved_at is set exactly when the incident is resolved
        CheckConstraint(
            "(status = 'resolved') = (resolved_at IS NOT NULL)",
            name="incidents_resolved_at_matches_status",
        ),
        CheckConstraint(
            "resolved_at IS NULL OR resolved_at >= started_at",
            name="incidents_resolved_after_start",
        ),
        CheckConstraint(
            "acknowledged_at IS NULL OR acknowledged_at >= started_at",
            name="incidents_acknowledged_after_start",
        ),
        # At most one unresolved incident per monitor, so two workers that notice
        # the same failure at once can't create duplicate incidents.
        Index(
            "incidents_one_active_per_monitor",
            "monitor_id",
            unique=True,
            postgresql_where=text("status <> 'resolved'"),
            sqlite_where=text("status <> 'resolved'"),
        ),
        Index("incidents_org_status_started_idx", "organization_id", "status", "started_at"),
        # A monitor's incident history (monitor pages, status page)
        Index("incidents_monitor_started_idx", "monitor_id", "started_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Must be a monitor of the same organization: the incident service checks this
    monitor_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("monitors.id", ondelete="CASCADE"),
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=IncidentStatus.OPEN.value
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    cause: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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

    events: Mapped[list["IncidentEvent"]] = relationship(
        back_populates="incident",
        cascade="all, delete-orphan",
        order_by="IncidentEvent.created_at",
    )


class IncidentEvent(Base):
    """One entry on an incident's timeline."""

    __tablename__ = "incident_events"
    __table_args__ = (
        CheckConstraint(
            f"event_type IN ({_sql_in(IncidentEventType)})",
            name="incident_events_type_valid",
        ),
        CheckConstraint(
            "event_type <> 'note' OR message IS NOT NULL",
            name="incident_events_note_has_message",
        ),
        Index("incident_events_incident_created_idx", "incident_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    incident_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("incidents.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Who acknowledged/resolved/wrote the note. Empty for automatic events.
    actor_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    incident: Mapped[Incident] = relationship(back_populates="events")
