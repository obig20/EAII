"""
0004_stop_ship_remediation

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-26 15:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Create auth_failures table for unknown-identifier attempts (SEC-12, SEC-06)
    op.create_table(
        "auth_failures",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("identifier_hmac", sa.String(64), nullable=False),
        sa.Column("ip_address", sa.Text, nullable=True),
        sa.Column("failed_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_auth_failures_hmac", "auth_failures", ["identifier_hmac"])
    op.create_index("ix_auth_failures_failed_at", "auth_failures", ["failed_at"])

    # 2. Add idempotency columns to import_batches and payment_lines (SEC-10)
    op.add_column("import_batches", sa.Column("file_hash", sa.String(64), nullable=True))
    op.create_unique_constraint("uq_import_batches_tenant_file_hash", "import_batches", ["tenant_id", "file_hash"])

    op.add_column("payment_lines", sa.Column("row_key", sa.String(128), nullable=True))
    op.create_unique_constraint("uq_payment_lines_tenant_row_key", "payment_lines", ["tenant_id", "row_key"])

    # Global unique indexes for identifiers across all tenants (SEC-09)
    conn.execute(sa.text("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_users_global_email 
        ON users (LOWER(TRIM(email))) 
        WHERE email IS NOT NULL
    """))
    conn.execute(sa.text("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_users_global_phone 
        ON users (TRIM(phone)) 
        WHERE phone IS NOT NULL
    """))

    # 3. Tighten RLS policies on sessions table (SEC-09)
    conn.execute(sa.text("DROP POLICY IF EXISTS sessions_select_policy ON sessions"))
    conn.execute(sa.text("""
        CREATE POLICY sessions_select_policy ON sessions
        FOR SELECT
        USING (tenant_id = NULLIF(current_setting('app.current_tenant', TRUE), '')::uuid)
    """))

    # 4. Dedicated NOLOGIN role to own SECURITY DEFINER functions (SEC-09)
    conn.execute(sa.text("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'auth_lookup_role') THEN
                CREATE ROLE auth_lookup_role WITH NOLOGIN BYPASSRLS;
            ELSE
                ALTER ROLE auth_lookup_role WITH NOLOGIN BYPASSRLS;
            END IF;
            GRANT USAGE ON SCHEMA public TO auth_lookup_role;
            REVOKE ALL ON users FROM auth_lookup_role;
            REVOKE ALL ON sessions FROM auth_lookup_role;
            REVOKE ALL ON telegram_invites FROM auth_lookup_role;
            GRANT SELECT (id, tenant_id, role, display_name, email, phone, password_hash, is_active, failed_login_attempts, locked_until, telegram_id) ON users TO auth_lookup_role;
            GRANT SELECT (id, tenant_id, user_id, expires_at, last_activity_at, token_hash) ON sessions TO auth_lookup_role;
            GRANT SELECT (id, tenant_id, role, display_name, expires_at, used_at, code_hash) ON telegram_invites TO auth_lookup_role;
        END
        $$;
    """))

    # Narrow SECURITY DEFINER functions for pre-auth lookup under app role (SEC-09)
    conn.execute(sa.text("""
        CREATE OR REPLACE FUNCTION lookup_user_by_identifier(p_identifier text)
        RETURNS TABLE (
            id uuid,
            tenant_id uuid,
            role varchar,
            display_name text,
            email varchar,
            phone varchar,
            password_hash text,
            is_active boolean,
            failed_login_attempts integer,
            locked_until timestamp with time zone
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
        DECLARE
            v_count integer;
        BEGIN
            SELECT count(*)
            INTO v_count
            FROM users u
            WHERE (LOWER(TRIM(u.email)) = LOWER(TRIM(p_identifier)) OR TRIM(u.phone) = TRIM(p_identifier));

            IF v_count > 1 THEN
                RAISE EXCEPTION 'Ambiguous user identifier: multiple accounts found' USING ERRCODE = 'data_exception';
            END IF;

            RETURN QUERY
            SELECT 
                u.id,
                u.tenant_id,
                u.role,
                u.display_name,
                u.email,
                u.phone,
                u.password_hash,
                u.is_active,
                u.failed_login_attempts,
                u.locked_until
            FROM users u
            WHERE (LOWER(TRIM(u.email)) = LOWER(TRIM(p_identifier)) OR TRIM(u.phone) = TRIM(p_identifier))
            LIMIT 1;
        END;
        $$;
    """))

    conn.execute(sa.text("""
        CREATE OR REPLACE FUNCTION lookup_session_by_token_hash(p_token_hash text)
        RETURNS TABLE (
            id uuid,
            tenant_id uuid,
            user_id uuid,
            expires_at timestamp with time zone,
            last_activity_at timestamp with time zone
        )
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
            SELECT 
                s.id,
                s.tenant_id,
                s.user_id,
                s.expires_at,
                s.last_activity_at
            FROM sessions s
            WHERE s.token_hash = p_token_hash
            LIMIT 1;
        $$;
    """))

    conn.execute(sa.text("""
        CREATE OR REPLACE FUNCTION lookup_user_by_telegram_id(p_telegram_id bigint)
        RETURNS TABLE (
            id uuid,
            tenant_id uuid,
            role varchar,
            display_name text,
            is_active boolean
        )
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
            SELECT 
                u.id,
                u.tenant_id,
                u.role,
                u.display_name,
                u.is_active
            FROM users u
            WHERE u.telegram_id = p_telegram_id
            LIMIT 1;
        $$;
    """))

    conn.execute(sa.text("""
        CREATE OR REPLACE FUNCTION lookup_telegram_invite(p_code_hash text)
        RETURNS TABLE (
            id uuid,
            tenant_id uuid,
            role varchar,
            display_name text,
            expires_at timestamp with time zone,
            used_at timestamp with time zone
        )
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
            SELECT 
                ti.id,
                ti.tenant_id,
                ti.role,
                ti.display_name,
                ti.expires_at,
                ti.used_at
            FROM telegram_invites ti
            WHERE ti.code_hash = p_code_hash
            LIMIT 1;
        $$;
    """))

    # Set function ownership to dedicated auth_lookup_role and grant EXECUTE to app
    conn.execute(sa.text("""
        ALTER FUNCTION lookup_user_by_identifier(text) OWNER TO auth_lookup_role;
        ALTER FUNCTION lookup_session_by_token_hash(text) OWNER TO auth_lookup_role;
        ALTER FUNCTION lookup_user_by_telegram_id(bigint) OWNER TO auth_lookup_role;
        ALTER FUNCTION lookup_telegram_invite(text) OWNER TO auth_lookup_role;

        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'app') THEN
                GRANT EXECUTE ON FUNCTION lookup_user_by_identifier(text) TO app;
                GRANT EXECUTE ON FUNCTION lookup_session_by_token_hash(text) TO app;
                GRANT EXECUTE ON FUNCTION lookup_user_by_telegram_id(bigint) TO app;
                GRANT EXECUTE ON FUNCTION lookup_telegram_invite(text) TO app;
                GRANT SELECT, INSERT, UPDATE, DELETE ON auth_failures TO app;
            END IF;
        END
        $$;
    """))

    # 5. Database Allocation Enforcement Trigger (SEC-02)
    # Only confirmed matches count toward allocation totals. Proposals remain insertable.
    conn.execute(sa.text("""
        CREATE OR REPLACE FUNCTION check_payment_match_allocations()
        RETURNS TRIGGER AS $$
        DECLARE
            v_payment_total bigint;
            v_payment_limit bigint;
            v_invoice_total bigint;
            v_invoice_limit bigint;
        BEGIN
            -- Only validate allocations for confirmed matches
            IF NEW.state <> 'confirmed' THEN
                RETURN NEW;
            END IF;

            -- 1. Check payment line cumulative confirmed allocation
            SELECT coalesce(sum(pm.matched_santim), 0)
            INTO v_payment_total
            FROM payment_matches pm
            WHERE pm.payment_line_id = NEW.payment_line_id
              AND pm.state = 'confirmed';

            SELECT pl.amount_santim
            INTO v_payment_limit
            FROM payment_lines pl
            WHERE pl.id = NEW.payment_line_id;

            IF v_payment_limit IS NOT NULL AND v_payment_total > v_payment_limit THEN
                RAISE EXCEPTION 'Total confirmed allocated santim (%) exceeds payment line amount (%)', v_payment_total, v_payment_limit
                    USING ERRCODE = 'check_violation';
            END IF;

            -- 2. Check invoice cumulative confirmed allocation
            SELECT coalesce(sum(pm.matched_santim), 0)
            INTO v_invoice_total
            FROM payment_matches pm
            WHERE pm.invoice_id = NEW.invoice_id
              AND pm.state = 'confirmed';

            SELECT inv.amount_santim
            INTO v_invoice_limit
            FROM invoices inv
            WHERE inv.id = NEW.invoice_id;

            IF v_invoice_limit IS NOT NULL AND v_invoice_total > v_invoice_limit THEN
                RAISE EXCEPTION 'Total confirmed allocated santim (%) exceeds invoice amount (%)', v_invoice_total, v_invoice_limit
                    USING ERRCODE = 'check_violation';
            END IF;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """))

    conn.execute(sa.text("DROP TRIGGER IF EXISTS trg_payment_match_allocations ON payment_matches"))
    conn.execute(sa.text("""
        CREATE TRIGGER trg_payment_match_allocations
        AFTER INSERT OR UPDATE ON payment_matches
        FOR EACH ROW
        EXECUTE FUNCTION check_payment_match_allocations()
    """))

    # 6. Dedicated read-only backup role with BYPASSRLS (SEC-01)
    conn.execute(sa.text("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'backup_role') THEN
                CREATE ROLE backup_role WITH LOGIN PASSWORD 'backup_secret_pass_123' BYPASSRLS;
            ELSE
                ALTER ROLE backup_role WITH BYPASSRLS;
            END IF;
            GRANT CONNECT ON DATABASE dailyclose_test TO backup_role;
            GRANT USAGE ON SCHEMA public TO backup_role;
            GRANT SELECT ON ALL TABLES IN SCHEMA public TO backup_role;
            GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO backup_role;
            ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO backup_role;
            ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON SEQUENCES TO backup_role;
        EXCEPTION WHEN OTHERS THEN
            NULL;
        END
        $$;
    """))


def downgrade() -> None:
    conn = op.get_bind()

    conn.execute(sa.text("DROP TRIGGER IF EXISTS trg_payment_match_allocations ON payment_matches"))
    conn.execute(sa.text("DROP FUNCTION IF EXISTS check_payment_match_allocations()"))

    conn.execute(sa.text("DROP FUNCTION IF EXISTS lookup_telegram_invite(text)"))
    conn.execute(sa.text("DROP FUNCTION IF EXISTS lookup_user_by_telegram_id(bigint)"))
    conn.execute(sa.text("DROP FUNCTION IF EXISTS lookup_session_by_token_hash(text)"))
    conn.execute(sa.text("DROP FUNCTION IF EXISTS lookup_user_by_identifier(text)"))

    conn.execute(sa.text("DROP INDEX IF EXISTS uq_users_global_phone"))
    conn.execute(sa.text("DROP INDEX IF EXISTS uq_users_global_email"))

    op.drop_constraint("uq_payment_lines_tenant_row_key", "payment_lines", type_="unique")
    op.drop_column("payment_lines", "row_key")

    op.drop_constraint("uq_import_batches_tenant_file_hash", "import_batches", type_="unique")
    op.drop_column("import_batches", "file_hash")

    op.drop_table("auth_failures")
