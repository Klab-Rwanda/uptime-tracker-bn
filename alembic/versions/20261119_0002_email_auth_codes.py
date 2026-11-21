"""Add email verification and password reset codes.

Revision ID: 20261119_0002
Revises: 20261118_0001
Create Date: 2026-11-19
"""

from alembic import op

revision = "20261119_0002"
down_revision = "20261118_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """ALTER TABLE users
        ADD COLUMN is_email_verified BOOLEAN NOT NULL DEFAULT TRUE"""
    )
    op.execute(
        """ALTER TABLE users
        ALTER COLUMN is_email_verified SET DEFAULT FALSE"""
    )
    op.execute(
        """CREATE TABLE auth_codes (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            purpose VARCHAR(32) NOT NULL,
            code_hash BYTEA NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            consumed_at TIMESTAMPTZ NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT auth_codes_purpose_valid
                CHECK (purpose IN ('email_verification', 'password_reset')),
            CONSTRAINT auth_codes_attempts_valid
                CHECK (attempts >= 0 AND attempts <= 5)
        )"""
    )
    op.execute(
        """CREATE INDEX auth_codes_user_purpose_created_idx
        ON auth_codes (user_id, purpose, created_at)"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE auth_codes")
    op.execute("ALTER TABLE users DROP COLUMN is_email_verified")
