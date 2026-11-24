"""Create api_keys table.

Revision ID: 20261123_0005
Revises: 20261123_0004
Create Date: 2026-11-23
"""

from alembic import op

revision = "20261123_0005"
down_revision = "20261123_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    statements = (
        """CREATE TABLE api_keys (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            name VARCHAR(255) NOT NULL,
            key_prefix VARCHAR(16) NOT NULL,
            key_hash BYTEA NOT NULL UNIQUE,
            scopes TEXT NOT NULL DEFAULT '["monitors:read","monitors:write"]',
            created_by_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            last_used_at TIMESTAMPTZ NULL,
            revoked_at TIMESTAMPTZ NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        """CREATE INDEX api_keys_org_idx
            ON api_keys (organization_id)""",
        """CREATE INDEX api_keys_hash_idx
            ON api_keys (key_hash)""",
    )
    for statement in statements:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE api_keys")
