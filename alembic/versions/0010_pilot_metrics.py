"""
0010_pilot_metrics

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-12 15:20:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Create pilot_snapshots table
    op.create_table(
        "pilot_snapshots",
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
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column(
            "snapshot_type",
            sa.String(50),
            nullable=False,
            server_default="baseline",
        ),
        sa.Column(
            "total_credit_sales_santim",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "total_overdue_santim",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "invoice_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "retailer_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "metrics_payload",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    op.create_index(
        "ix_pilot_snapshots_tenant_id",
        "pilot_snapshots",
        ["tenant_id"],
    )
    op.create_index(
        "ix_pilot_snapshots_tenant_type",
        "pilot_snapshots",
        ["tenant_id", "snapshot_type"],
    )
    op.create_index(
        "ix_pilot_snapshots_tenant_date",
        "pilot_snapshots",
        ["tenant_id", "snapshot_date"],
    )

    # 2. RLS Policies and Permissions
    if conn.dialect.name == "postgresql":
        conn.execute(sa.text("""
            ALTER TABLE pilot_snapshots ENABLE ROW LEVEL SECURITY;
            ALTER TABLE pilot_snapshots FORCE ROW LEVEL SECURITY;

            CREATE POLICY pilot_snapshots_isolation ON pilot_snapshots
                FOR ALL
                TO app
                USING (
                    tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid
                )
                WITH CHECK (
                    tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid
                );

            GRANT SELECT, INSERT, UPDATE, DELETE ON pilot_snapshots TO app;
            GRANT SELECT ON pilot_snapshots TO backup_role;
        """))


def downgrade() -> None:
    conn = op.get_bind()

    if conn.dialect.name == "postgresql":
        conn.execute(sa.text("""
            DROP POLICY IF EXISTS pilot_snapshots_isolation ON pilot_snapshots;
        """))

    op.drop_table("pilot_snapshots")
