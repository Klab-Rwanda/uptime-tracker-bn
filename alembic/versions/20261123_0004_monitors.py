"""Create monitors table.

Revision ID: 20261123_0004
Revises: 20261123_0003
Create Date: 2026-11-23
"""

from alembic import op

revision = "20261123_0004"
down_revision = "20261123_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    statements = (
        """CREATE TABLE monitors (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            project_id UUID NULL REFERENCES projects(id) ON DELETE SET NULL,
            name VARCHAR(255) NOT NULL,
            monitor_type VARCHAR(10) NOT NULL,
            target TEXT NOT NULL,
            method VARCHAR(10) NULL DEFAULT 'GET',
            expected_status_codes VARCHAR(255) NULL,
            keyword TEXT NULL,
            timeout_seconds INTEGER NOT NULL DEFAULT 10,
            retry_count INTEGER NOT NULL DEFAULT 2,
            retry_interval_seconds INTEGER NOT NULL DEFAULT 30,
            check_interval_seconds INTEGER NOT NULL DEFAULT 60,
            regions TEXT NULL,
            notification_channel_ids TEXT NULL,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            current_status VARCHAR(20) NOT NULL DEFAULT 'pending',
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            consecutive_successes INTEGER NOT NULL DEFAULT 0,
            last_checked_at TIMESTAMPTZ NULL,
            next_check_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        """CREATE INDEX monitors_org_project_idx
            ON monitors (organization_id, project_id)""",
        """CREATE INDEX monitors_next_check_idx
            ON monitors (is_active, next_check_at)""",
    )
    for statement in statements:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE monitors")
