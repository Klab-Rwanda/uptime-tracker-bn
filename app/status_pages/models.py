from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.util.database import Base


class StatusPage(Base):
    """A public page showing the health of some of an organization's monitors."""

    __tablename__ = "status_pages"
    __table_args__ = (Index("status_pages_org_idx", "organization_id"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    # Public address (/status/<slug>): unique across the whole platform
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    custom_domain: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    groups: Mapped[list["StatusPageGroup"]] = relationship(
        back_populates="page",
        cascade="all, delete-orphan",
        order_by="StatusPageGroup.position",
        lazy="selectin",
    )


class StatusPageGroup(Base):
    """A named section of a status page, e.g. "Customer-facing services"."""

    __tablename__ = "status_page_groups"
    __table_args__ = (
        UniqueConstraint("status_page_id", "position", name="status_page_groups_position_unique"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    status_page_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("status_pages.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    page: Mapped[StatusPage] = relationship(back_populates="groups")
    monitors: Mapped[list["StatusPageMonitor"]] = relationship(
        back_populates="group",
        cascade="all, delete-orphan",
        order_by="StatusPageMonitor.position",
        lazy="selectin",
    )


class StatusPageMonitor(Base):
    """A monitor shown in a group. Deleting the monitor removes it from pages."""

    __tablename__ = "status_page_monitors"
    __table_args__ = (Index("status_page_monitors_monitor_idx", "monitor_id"),)

    group_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("status_page_groups.id", ondelete="CASCADE"),
        primary_key=True,
    )
    monitor_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("monitors.id", ondelete="CASCADE"),
        primary_key=True,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    group: Mapped[StatusPageGroup] = relationship(back_populates="monitors")
