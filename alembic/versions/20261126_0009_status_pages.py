"""Create status_pages, status_page_groups and status_page_monitors tables.

Revision ID: 20261126_0009
Revises: 20261125_0008
Create Date: 2026-11-26
"""

from alembic import op

revision = "20261126_0009"
down_revision = "20261125_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    statements = (
        """CREATE TABLE status_pages (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            title VARCHAR(255) NOT NULL,
            slug VARCHAR(64) NOT NULL,
            custom_domain VARCHAR(255) NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT status_pages_slug_key UNIQUE (slug),
            CONSTRAINT status_pages_custom_domain_key UNIQUE (custom_domain)
        )""",
        """CREATE INDEX status_pages_org_idx ON status_pages (organization_id)""",
        """CREATE TABLE status_page_groups (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            status_page_id UUID NOT NULL REFERENCES status_pages(id) ON DELETE CASCADE,
            name VARCHAR(100) NOT NULL,
            position INTEGER NOT NULL,
            CONSTRAINT status_page_groups_position_unique UNIQUE (status_page_id, position)
        )""",
        """CREATE TABLE status_page_monitors (
            group_id UUID NOT NULL REFERENCES status_page_groups(id) ON DELETE CASCADE,
            monitor_id UUID NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
            position INTEGER NOT NULL,
            PRIMARY KEY (group_id, monitor_id)
        )""",
        """CREATE INDEX status_page_monitors_monitor_idx ON status_page_monitors (monitor_id)""",
    )
    for statement in statements:
        op.execute(statement)


def downgrade() -> None:
    for statement in (
        "DROP TABLE status_page_monitors",
        "DROP TABLE status_page_groups",
        "DROP TABLE status_pages",
    ):
        op.execute(statement)
