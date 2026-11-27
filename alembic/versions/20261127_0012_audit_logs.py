"""Create audit_logs table.

Revision ID: 20261127_0012
Revises: 20261127_0011
Create Date: 2026-11-27
"""

from alembic import op

revision = "20261127_0012"
down_revision = "20261127_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE TABLE audit_logs (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            actor_user_id UUID NULL REFERENCES users(id) ON DELETE SET NULL,
            actor_api_key_id UUID NULL,
            action VARCHAR(64) NOT NULL,
            target_type VARCHAR(32) NOT NULL,
            target_id UUID NULL,
            details JSONB NOT NULL,
            request_id VARCHAR(64) NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )"""
    )
    op.execute(
        "CREATE INDEX audit_logs_org_created_idx ON audit_logs (organization_id, created_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE audit_logs")
