"""Create notification_channels table.

Revision ID: 20261124_0006
Revises: 20261123_0005
Create Date: 2026-11-24
"""

from alembic import op

revision = "20261124_0006"
down_revision = "20261123_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE TABLE notification_channels (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            name VARCHAR(255) NOT NULL,
            channel_type VARCHAR(16) NOT NULL,
            config_encrypted TEXT NOT NULL,
            created_by_user_id UUID NULL REFERENCES users(id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT notification_channels_type_valid
                CHECK (channel_type IN ('webhook', 'email')),
            CONSTRAINT notification_channels_org_name_unique
                UNIQUE (organization_id, name)
        )"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE notification_channels")
