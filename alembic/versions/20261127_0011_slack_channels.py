"""Allow Slack notification channels.

Revision ID: 20261127_0011
Revises: 20261126_0010
Create Date: 2026-11-27
"""

from alembic import op

revision = "20261127_0011"
down_revision = "20261126_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE notification_channels DROP CONSTRAINT notification_channels_type_valid"
    )
    op.execute(
        """ALTER TABLE notification_channels ADD CONSTRAINT notification_channels_type_valid
            CHECK (channel_type IN ('webhook', 'email', 'slack'))"""
    )


def downgrade() -> None:
    # Fails while Slack channels exist: delete them first (on purpose, so a
    # downgrade never silently throws away customers' channels)
    op.execute(
        "ALTER TABLE notification_channels DROP CONSTRAINT notification_channels_type_valid"
    )
    op.execute(
        """ALTER TABLE notification_channels ADD CONSTRAINT notification_channels_type_valid
            CHECK (channel_type IN ('webhook', 'email'))"""
    )
