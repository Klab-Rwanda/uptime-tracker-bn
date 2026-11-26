"""Create maintenance_windows and maintenance_window_monitors tables.

Revision ID: 20261126_0010
Revises: 20261126_0009
Create Date: 2026-11-26
"""

from alembic import op

revision = "20261126_0010"
down_revision = "20261126_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    statements = (
        """CREATE TABLE maintenance_windows (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            name VARCHAR(255) NOT NULL,
            starts_at TIMESTAMPTZ NOT NULL,
            ends_at TIMESTAMPTZ NOT NULL,
            suppress_notifications BOOLEAN NOT NULL DEFAULT true,
            created_by_user_id UUID NULL REFERENCES users(id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT maintenance_windows_ends_after_start CHECK (ends_at > starts_at)
        )""",
        """CREATE INDEX maintenance_windows_org_starts_idx
            ON maintenance_windows (organization_id, starts_at)""",
        """CREATE TABLE maintenance_window_monitors (
            window_id UUID NOT NULL REFERENCES maintenance_windows(id) ON DELETE CASCADE,
            monitor_id UUID NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
            PRIMARY KEY (window_id, monitor_id)
        )""",
        """CREATE INDEX maintenance_window_monitors_monitor_idx
            ON maintenance_window_monitors (monitor_id)""",
    )
    for statement in statements:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE maintenance_window_monitors")
    op.execute("DROP TABLE maintenance_windows")
