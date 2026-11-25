"""Create user authentication and session tables.

Revision ID: 20261118_0001
Revises:
Create Date: 2026-11-18
"""

from alembic import op

revision = "20261118_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    statements = (
        'CREATE EXTENSION IF NOT EXISTS "pgcrypto"',
        """CREATE TABLE users (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            email VARCHAR(255) NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            full_name VARCHAR(255) NOT NULL,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            is_superuser BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT users_email_lowercase CHECK (email = lower(email))
        )""",
        """CREATE FUNCTION set_user_updated_at() RETURNS trigger
            LANGUAGE plpgsql AS $updated$
            BEGIN
                NEW.updated_at := now();
                RETURN NEW;
            END
            $updated$""",
        """CREATE TRIGGER users_set_updated_at
            BEFORE UPDATE ON users
            FOR EACH ROW EXECUTE FUNCTION set_user_updated_at()""",
        """CREATE TABLE auth_sessions (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            expires_at TIMESTAMPTZ NOT NULL,
            revoked_at TIMESTAMPTZ NULL,
            last_used_at TIMESTAMPTZ NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT auth_sessions_id_user_unique UNIQUE (id, user_id),
            CONSTRAINT auth_sessions_expiry_valid
                CHECK (expires_at > created_at),
            CONSTRAINT auth_sessions_revocation_valid
                CHECK (revoked_at IS NULL OR revoked_at >= created_at)
        )""",
        """CREATE INDEX auth_sessions_user_active_idx
            ON auth_sessions (user_id, expires_at)
            WHERE revoked_at IS NULL""",
        """CREATE TABLE refresh_tokens (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID NOT NULL,
            auth_session_id UUID NOT NULL,
            token_hash BYTEA NOT NULL UNIQUE,
            expires_at TIMESTAMPTZ NOT NULL,
            revoked_at TIMESTAMPTZ NULL,
            last_used_at TIMESTAMPTZ NULL,
            replaced_by_token_id UUID NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT refresh_tokens_session_same_user
                FOREIGN KEY (auth_session_id, user_id)
                REFERENCES auth_sessions (id, user_id) ON DELETE CASCADE,
            CONSTRAINT refresh_tokens_id_user_session_unique
                UNIQUE (id, user_id, auth_session_id),
            CONSTRAINT refresh_tokens_replacement_same_session
                FOREIGN KEY (replaced_by_token_id, user_id, auth_session_id)
                REFERENCES refresh_tokens (id, user_id, auth_session_id)
                DEFERRABLE INITIALLY DEFERRED,
            CONSTRAINT refresh_tokens_hash_length
                CHECK (length(token_hash) = 32),
            CONSTRAINT refresh_tokens_expiry_valid
                CHECK (expires_at > created_at),
            CONSTRAINT refresh_tokens_revocation_valid
                CHECK (revoked_at IS NULL OR revoked_at >= created_at),
            CONSTRAINT refresh_tokens_no_self_replacement
                CHECK (replaced_by_token_id IS NULL OR replaced_by_token_id <> id)
        )""",
        """CREATE INDEX refresh_tokens_session_expiry_idx
            ON refresh_tokens (auth_session_id, expires_at)""",
        """CREATE INDEX refresh_tokens_user_idx
            ON refresh_tokens (user_id)""",
    )
    for statement in statements:
        op.execute(statement)


def downgrade() -> None:
    statements = (
        "DROP TABLE refresh_tokens",
        "DROP TABLE auth_sessions",
        "DROP TRIGGER users_set_updated_at ON users",
        "DROP FUNCTION set_user_updated_at()",
        "DROP TABLE users",
    )
    for statement in statements:
        op.execute(statement)
