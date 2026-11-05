"""
0003_auth_sessions_ratelimit

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-26 11:20:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Extend users table
    op.add_column("users", sa.Column("email", sa.String(255), nullable=True))
    op.add_column("users", sa.Column("phone", sa.String(50), nullable=True))
    op.add_column("users", sa.Column("password_hash", sa.Text, nullable=True))
    op.add_column("users", sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")))
    op.add_column("users", sa.Column("failed_login_attempts", sa.Integer, nullable=False, server_default=sa.text("0")))
    op.add_column("users", sa.Column("locked_until", sa.TIMESTAMP(timezone=True), nullable=True))

    op.create_index("ix_users_tenant_email", "users", ["tenant_id", "email"])
    op.create_index("ix_users_tenant_phone", "users", ["tenant_id", "phone"])

    # 2. Create sessions table
    op.create_table(
        "sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.Text, nullable=False, unique=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("last_activity_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("user_agent", sa.Text, nullable=True),
        sa.Column("ip_address", sa.Text, nullable=True),
    )
    op.create_index("ix_sessions_tenant_id", "sessions", ["tenant_id"])
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_index("ix_sessions_token_hash", "sessions", ["token_hash"])

    # 3. Create rate_limit_buckets table (infrastructure storage for rate limits)
    op.create_table(
        "rate_limit_buckets",
        sa.Column("key", sa.String(255), primary_key=True),
        sa.Column("attempts", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("first_attempt_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("last_attempt_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("locked_until", sa.TIMESTAMP(timezone=True), nullable=True),
    )

    # 4. Create telegram_invites table
    op.create_table(
        "telegram_invites",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("code_hash", sa.Text, nullable=False, unique=True),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("display_name", sa.Text, nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("used_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("used_by_telegram_id", sa.BigInteger, nullable=True),
    )
    op.create_index("ix_telegram_invites_tenant_id", "telegram_invites", ["tenant_id"])

    # 5. Row-Level Security for sessions & telegram_invites
    conn.execute(sa.text("ALTER TABLE sessions ENABLE ROW LEVEL SECURITY"))
    conn.execute(sa.text("ALTER TABLE sessions FORCE ROW LEVEL SECURITY"))
    conn.execute(sa.text("""
        CREATE POLICY sessions_select_policy ON sessions
        FOR SELECT
        USING (
            tenant_id = NULLIF(current_setting('app.current_tenant', TRUE), '')::uuid
            OR NULLIF(current_setting('app.current_tenant', TRUE), '') IS NULL
        )
    """))
    conn.execute(sa.text("""
        CREATE POLICY sessions_write_policy ON sessions
        FOR ALL
        USING (tenant_id = NULLIF(current_setting('app.current_tenant', TRUE), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', TRUE), '')::uuid)
    """))

    conn.execute(sa.text("ALTER TABLE telegram_invites ENABLE ROW LEVEL SECURITY"))
    conn.execute(sa.text("ALTER TABLE telegram_invites FORCE ROW LEVEL SECURITY"))
    conn.execute(sa.text("""
        CREATE POLICY telegram_invites_select_policy ON telegram_invites
        FOR SELECT
        USING (
            tenant_id = NULLIF(current_setting('app.current_tenant', TRUE), '')::uuid
            OR NULLIF(current_setting('app.current_tenant', TRUE), '') IS NULL
        )
    """))
    conn.execute(sa.text("""
        CREATE POLICY telegram_invites_write_policy ON telegram_invites
        FOR ALL
        USING (tenant_id = NULLIF(current_setting('app.current_tenant', TRUE), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', TRUE), '')::uuid)
    """))

    # 6. Grant permissions to app role
    conn.execute(sa.text("""
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'app') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON sessions TO app;
                GRANT SELECT, INSERT, UPDATE, DELETE ON rate_limit_buckets TO app;
                GRANT SELECT, INSERT, UPDATE, DELETE ON telegram_invites TO app;
            END IF;
        END
        $$;
    """))


def downgrade() -> None:
    conn = op.get_bind()

    op.drop_table("telegram_invites")
    op.drop_table("rate_limit_buckets")
    op.drop_table("sessions")

    op.drop_index("ix_users_tenant_phone", "users")
    op.drop_index("ix_users_tenant_email", "users")
    op.drop_column("users", "locked_until")
    op.drop_column("users", "failed_login_attempts")
    op.drop_column("users", "is_active")
    op.drop_column("users", "password_hash")
    op.drop_column("users", "phone")
    op.drop_column("users", "email")
