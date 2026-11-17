"""
0011_payer_aliases

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-17 14:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Create payer_aliases table
    op.create_table(
        "payer_aliases",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "retailer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("retailers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("normalized_payer_name", sa.String(255), nullable=True),
        sa.Column("normalized_payer_ref", sa.String(100), nullable=True),
        sa.Column("raw_payer_name", sa.String(255), nullable=True),
        sa.Column("raw_payer_ref", sa.String(100), nullable=True),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "deleted_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )

    op.create_index(
        "ix_payer_aliases_tenant_id",
        "payer_aliases",
        ["tenant_id"],
    )
    op.create_index(
        "ix_payer_aliases_retailer_id",
        "payer_aliases",
        ["retailer_id"],
    )
    op.create_index(
        "ix_payer_aliases_lookup",
        "payer_aliases",
        ["tenant_id", "normalized_payer_name", "normalized_payer_ref"],
    )

    # 2. RLS Policies and Permissions
    if conn.dialect.name == "postgresql":
        conn.execute(sa.text("""
            ALTER TABLE payer_aliases ENABLE ROW LEVEL SECURITY;
            ALTER TABLE payer_aliases FORCE ROW LEVEL SECURITY;

            CREATE POLICY payer_aliases_isolation ON payer_aliases
                FOR ALL
                TO app
                USING (
                    tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid
                )
                WITH CHECK (
                    tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid
                );

            GRANT SELECT, INSERT, UPDATE, DELETE ON payer_aliases TO app;
            GRANT SELECT ON payer_aliases TO backup_role;
        """))


def downgrade() -> None:
    conn = op.get_bind()

    if conn.dialect.name == "postgresql":
        conn.execute(sa.text("""
            DROP POLICY IF EXISTS payer_aliases_isolation ON payer_aliases;
        """))

    op.drop_table("payer_aliases")
