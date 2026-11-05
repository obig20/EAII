"""
0002_security_hardening

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-26 10:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # Drop existing unified policy on retailer_confirmation_tokens
    conn.execute(sa.text("DROP POLICY IF EXISTS tenant_isolation ON retailer_confirmation_tokens"))

    # Public token resolution policy:
    # 1. When tenant context is set (e.g. app.current_tenant = Tenant_B), only Tenant_B tokens are accessible.
    # 2. When tenant context is unset (NULL/empty, e.g. public retailer confirmation lookup),
    #    tokens can be looked up by unique 256-bit SHA-256 hash.
    conn.execute(sa.text("""
        CREATE POLICY retailer_tokens_select_policy ON retailer_confirmation_tokens
        FOR SELECT
        USING (
            tenant_id = NULLIF(current_setting('app.current_tenant', TRUE), '')::uuid
            OR NULLIF(current_setting('app.current_tenant', TRUE), '') IS NULL
        )
    """))

    # Write policy: strictly scoped to active tenant context
    conn.execute(sa.text("""
        CREATE POLICY retailer_tokens_write_policy ON retailer_confirmation_tokens
        FOR ALL
        USING (tenant_id = NULLIF(current_setting('app.current_tenant', TRUE), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', TRUE), '')::uuid)
    """))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP POLICY IF EXISTS retailer_tokens_select_policy ON retailer_confirmation_tokens"))
    conn.execute(sa.text("DROP POLICY IF EXISTS retailer_tokens_write_policy ON retailer_confirmation_tokens"))
    conn.execute(sa.text("""
        CREATE POLICY tenant_isolation ON retailer_confirmation_tokens
        USING (tenant_id = NULLIF(current_setting('app.current_tenant', TRUE), '')::uuid)
    """))
