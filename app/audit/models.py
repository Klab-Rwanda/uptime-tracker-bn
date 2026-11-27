from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.util.database import Base


class AuditLog(Base):
    """One important change: who did what to which object, and when.

    Rows are only ever added, never changed. They are written in the same
    transaction as the change, so a row exists exactly when the change happened.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("audit_logs_org_created_idx", "organization_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Who: kept as history even after the user or key is deleted
    actor_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_api_key_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    # What, e.g. "status_page.deleted"
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # No foreign key: the target may be deleted, the record of it stays
    target_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    # Names and changed field names only: never secrets, URLs or configs
    details: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False, default=dict
    )
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Set by Python too: microseconds keep entries of the same second in order
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        nullable=False,
    )
