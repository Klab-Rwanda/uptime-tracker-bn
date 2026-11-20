from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.util.database import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("email = lower(email)", name="users_email_lowercase"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_superuser: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_email_verified: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
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


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    __table_args__ = (
        UniqueConstraint("id", "user_id", name="auth_sessions_id_user_unique"),
        Index(
            "auth_sessions_user_active_idx",
            "user_id",
            "expires_at",
            postgresql_where=text("revoked_at IS NULL"),
        ),
        CheckConstraint(
            "expires_at > created_at",
            name="auth_sessions_expiry_valid",
        ),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name="auth_sessions_revocation_valid",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    __table_args__ = (
        UniqueConstraint(
            "id",
            "user_id",
            "auth_session_id",
            name="refresh_tokens_id_user_session_unique",
        ),
        ForeignKeyConstraint(
            ["auth_session_id", "user_id"],
            ["auth_sessions.id", "auth_sessions.user_id"],
            name="refresh_tokens_session_same_user",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["replaced_by_token_id", "user_id", "auth_session_id"],
            [
                "refresh_tokens.id",
                "refresh_tokens.user_id",
                "refresh_tokens.auth_session_id",
            ],
            name="refresh_tokens_replacement_same_session",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint("length(token_hash) = 32", name="refresh_tokens_hash_length"),
        CheckConstraint(
            "expires_at > created_at",
            name="refresh_tokens_expiry_valid",
        ),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name="refresh_tokens_revocation_valid",
        ),
        CheckConstraint(
            "replaced_by_token_id IS NULL OR replaced_by_token_id <> id",
            name="refresh_tokens_no_self_replacement",
        ),
        Index("refresh_tokens_session_expiry_idx", "auth_session_id", "expires_at"),
        Index("refresh_tokens_user_idx", "user_id"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    auth_session_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_by_token_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AuthCode(Base):
    __tablename__ = "auth_codes"
    __table_args__ = (
        CheckConstraint(
            "purpose IN ('email_verification', 'password_reset')",
            name="auth_codes_purpose_valid",
        ),
        CheckConstraint(
            "attempts >= 0 AND attempts <= 5",
            name="auth_codes_attempts_valid",
        ),
        Index("auth_codes_user_purpose_created_idx", "user_id", "purpose", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    code_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
