"""Create organizations, memberships, invitations, and projects tables.

Revision ID: 20261123_0003
Revises: 20261119_0002
Create Date: 2026-11-23
"""

from alembic import op

revision = "20261123_0003"
down_revision = "20261119_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    statements = (
        """CREATE TABLE organizations (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name VARCHAR(120) NOT NULL,
            slug VARCHAR(120) UNIQUE NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        """CREATE TABLE organization_memberships (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role VARCHAR(32) NOT NULL DEFAULT 'member',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT organization_memberships_role_valid
                CHECK (role IN ('owner', 'admin', 'editor', 'viewer', 'member')),
            CONSTRAINT organization_memberships_org_user_unique
                UNIQUE (organization_id, user_id)
        )""",
        """CREATE INDEX organization_memberships_user_idx
            ON organization_memberships (user_id)""",
        """CREATE UNIQUE INDEX organization_memberships_one_owner_idx
            ON organization_memberships (organization_id)
            WHERE role = 'owner'""",
        """CREATE TABLE organization_invitations (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            email VARCHAR(255) NOT NULL,
            role VARCHAR(32) NOT NULL DEFAULT 'editor',
            invited_by_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash BYTEA NOT NULL UNIQUE,
            expires_at TIMESTAMPTZ NOT NULL,
            accepted_at TIMESTAMPTZ NULL,
            revoked_at TIMESTAMPTZ NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT organization_invitations_email_lowercase
                CHECK (email = lower(email)),
            CONSTRAINT organization_invitations_token_hash_length
                CHECK (length(token_hash) = 32),
            CONSTRAINT organization_invitations_expiry_valid
                CHECK (expires_at > created_at),
            CONSTRAINT organization_invitations_terminal_state_valid
                CHECK (accepted_at IS NULL OR revoked_at IS NULL)
        )""",
        """CREATE UNIQUE INDEX organization_invitations_pending_org_email_idx
            ON organization_invitations (organization_id, email)
            WHERE accepted_at IS NULL AND revoked_at IS NULL""",
        """CREATE INDEX organization_invitations_org_created_idx
            ON organization_invitations (organization_id, created_at)""",
        """CREATE TABLE projects (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            name VARCHAR(255) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT projects_org_name_unique
                UNIQUE (organization_id, name)
        )""",
        """CREATE INDEX projects_org_idx
            ON projects (organization_id)""",
    )
    for statement in statements:
        op.execute(statement)


def downgrade() -> None:
    statements = (
        "DROP TABLE projects",
        "DROP TABLE organization_invitations",
        "DROP TABLE organization_memberships",
        "DROP TABLE organizations",
    )
    for statement in statements:
        op.execute(statement)
