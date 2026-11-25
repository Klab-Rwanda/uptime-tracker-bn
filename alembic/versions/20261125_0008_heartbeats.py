"""Create heartbeats table.

Revision ID: 20261125_0008
Revises: 20261125_0007
Create Date: 2026-11-25
"""

from alembic import op

revision = "20261125_0008"
down_revision = "20261125_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    statements = (
        """CREATE TABLE heartbeats (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            monitor_id UUID NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
            status VARCHAR(20) NOT NULL,
            response_time_ms INTEGER NULL,
            checked_at TIMESTAMPTZ NOT NULL,
            error_message TEXT NULL,
            CONSTRAINT heartbeats_status_valid CHECK (status IN ('up', 'down', 'pending'))
        )""",
        # The latest heartbeats of a monitor (incident detection, dashboards)
        """CREATE INDEX heartbeats_monitor_checked_idx
            ON heartbeats (monitor_id, checked_at)""",
    )
    for statement in statements:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE heartbeats")
