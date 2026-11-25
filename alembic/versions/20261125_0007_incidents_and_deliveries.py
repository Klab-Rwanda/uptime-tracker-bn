"""Create incidents, incident_events and notification_deliveries tables.

Revision ID: 20261125_0007
Revises: 20261124_0006
Create Date: 2026-11-25
"""

from alembic import op

revision = "20261125_0007"
down_revision = "20261124_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    statements = (
        """CREATE TABLE incidents (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            monitor_id UUID NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
            status VARCHAR(20) NOT NULL DEFAULT 'open',
            title VARCHAR(255) NOT NULL,
            cause TEXT NULL,
            started_at TIMESTAMPTZ NOT NULL,
            acknowledged_at TIMESTAMPTZ NULL,
            resolved_at TIMESTAMPTZ NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT incidents_status_valid
                CHECK (status IN ('open', 'acknowledged', 'resolved')),
            CONSTRAINT incidents_resolved_at_matches_status
                CHECK ((status = 'resolved') = (resolved_at IS NOT NULL)),
            CONSTRAINT incidents_resolved_after_start
                CHECK (resolved_at IS NULL OR resolved_at >= started_at),
            CONSTRAINT incidents_acknowledged_after_start
                CHECK (acknowledged_at IS NULL OR acknowledged_at >= started_at)
        )""",
        # At most one unresolved incident per monitor
        """CREATE UNIQUE INDEX incidents_one_active_per_monitor
            ON incidents (monitor_id)
            WHERE status <> 'resolved'""",
        """CREATE INDEX incidents_org_status_started_idx
            ON incidents (organization_id, status, started_at)""",
        """CREATE INDEX incidents_monitor_started_idx
            ON incidents (monitor_id, started_at)""",
        """CREATE TABLE incident_events (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            incident_id UUID NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
            event_type VARCHAR(20) NOT NULL,
            message TEXT NULL,
            actor_user_id UUID NULL REFERENCES users(id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT incident_events_type_valid
                CHECK (event_type IN ('opened', 'acknowledged', 'resolved', 'note')),
            CONSTRAINT incident_events_note_has_message
                CHECK (event_type <> 'note' OR message IS NOT NULL)
        )""",
        """CREATE INDEX incident_events_incident_created_idx
            ON incident_events (incident_id, created_at)""",
        # channel_id has no foreign key on purpose: deliveries are history and
        # must survive a deleted channel (see app/models/notification_delivery.py)
        """CREATE TABLE notification_deliveries (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            idempotency_key VARCHAR(255) NOT NULL,
            channel_id VARCHAR(64) NOT NULL,
            payload JSONB NOT NULL,
            status VARCHAR(20) NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TIMESTAMPTZ NOT NULL,
            lease_until TIMESTAMPTZ NULL,
            last_error TEXT NULL,
            provider_message_id VARCHAR(255) NULL,
            sent_at TIMESTAMPTZ NULL,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT notification_deliveries_idempotency_key_key UNIQUE (idempotency_key),
            CONSTRAINT notification_deliveries_status_valid
                CHECK (status IN ('pending', 'in_progress', 'sent', 'failed')),
            CONSTRAINT notification_deliveries_attempts_valid CHECK (attempts >= 0),
            CONSTRAINT notification_deliveries_in_progress_has_lease
                CHECK (status <> 'in_progress' OR lease_until IS NOT NULL),
            CONSTRAINT notification_deliveries_sent_at_matches_status
                CHECK ((status = 'sent') = (sent_at IS NOT NULL))
        )""",
        """CREATE INDEX notification_deliveries_due_idx
            ON notification_deliveries (status, next_attempt_at)""",
        """CREATE INDEX notification_deliveries_lease_idx
            ON notification_deliveries (status, lease_until)""",
    )
    for statement in statements:
        op.execute(statement)


def downgrade() -> None:
    statements = (
        "DROP TABLE notification_deliveries",
        "DROP TABLE incident_events",
        "DROP TABLE incidents",
    )
    for statement in statements:
        op.execute(statement)
