from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID

from app.models.base import Base


class Heartbeat(Base):
    __tablename__ = "heartbeats"

    id = Column(UUID(as_uuid=True), primary_key=True)
    monitor_id = Column(UUID(as_uuid=True), ForeignKey("monitors.id"), nullable=False)
    status = Column(String(20), nullable=False)
    response_time_ms = Column(Integer, nullable=True)
    checked_at = Column(DateTime(timezone=True), nullable=False)
    error_message = Column(Text, nullable=True)
