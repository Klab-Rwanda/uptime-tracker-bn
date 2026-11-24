from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID

from app.models.base import Base


class Monitor(Base):
    __tablename__ = "monitors"

    id = Column(UUID(as_uuid=True), primary_key=True)
    organization_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    name = Column(String(255), nullable=False)
    monitor_type = Column(String(10), nullable=False)
    target = Column(Text, nullable=False)
    method = Column(String(10), nullable=True)
    expected_status_codes = Column(String(255), nullable=True)
    keyword = Column(Text, nullable=True)
    timeout_seconds = Column(Integer, nullable=False, default=30)
    retry_count = Column(Integer, nullable=False, default=0)
    retry_interval_seconds = Column(Integer, nullable=False, default=60)
    check_interval_seconds = Column(Integer, nullable=False, default=60)
    is_active = Column(Boolean, nullable=False, default=True)
    current_status = Column(String(20), nullable=False, default="pending")
    consecutive_failures = Column(Integer, nullable=False, default=0)
    consecutive_successes = Column(Integer, nullable=False, default=0)
    last_checked_at = Column(DateTime(timezone=True), nullable=True)
    next_check_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)
