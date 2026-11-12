"""
0008_import_wizard

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-12 12:45:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Create tenant_import_mappings table
    op.create_table(
        "tenant_import_mappings",
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
        sa.Column("source_name", sa.String(100), nullable=False),
        sa.Column("delimiter", sa.String(5), nullable=False, server_default=","),
        sa.Column("encoding", sa.String(20), nullable=False, server_default="utf-8"),
        sa.Column("header_row_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("date_column", sa.String(100), nullable=False),
        sa.Column("date_format", sa.String(50), nullable=False, server_default="%Y-%m-%d"),
        sa.Column("amount_column", sa.String(100), nullable=True),
        sa.Column("credit_column", sa.String(100), nullable=True),
        sa.Column("debit_column", sa.String(100), nullable=True),
        sa.Column("reference_column", sa.String(100), nullable=True),
        sa.Column("narrative_column", sa.String(100), nullable=True),
        sa.Column("counterparty_name_column", sa.String(100), nullable=True),
        sa.Column("counterparty_account_column", sa.String(100), nullable=True),
        sa.Column("tx_id_column", sa.String(100), nullable=True),
        sa.Column("number_format", sa.String(20), nullable=False, server_default="1,234.56"),
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
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("tenant_id", "source_name", name="uq_tenant_source_mapping"),
    )

    op.create_index(
        "ix_tenant_import_mappings_tenant_id",
        "tenant_import_mappings",
        ["tenant_id"],
    )

    # 2. Create import_batch_errors table
    op.create_table(
        "import_batch_errors",
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
            "import_batch_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("import_batches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("row_index", sa.Integer(), nullable=False),
        sa.Column("raw_data", sa.Text(), nullable=False),
        sa.Column("error_reason", sa.String(255), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_index(
        "ix_import_batch_errors_tenant_id",
        "import_batch_errors",
        ["tenant_id"],
    )
    op.create_index(
        "ix_import_batch_errors_batch_id",
        "import_batch_errors",
        ["import_batch_id"],
    )

    # 3. RLS Policies and Permissions
    if conn.dialect.name == "postgresql":
        conn.execute(sa.text("""
            ALTER TABLE tenant_import_mappings ENABLE ROW LEVEL SECURITY;
            ALTER TABLE tenant_import_mappings FORCE ROW LEVEL SECURITY;

            CREATE POLICY tenant_import_mappings_isolation ON tenant_import_mappings
                FOR ALL
                TO app
                USING (
                    tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid
                )
                WITH CHECK (
                    tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid
                );

            GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_import_mappings TO app;
            GRANT SELECT ON tenant_import_mappings TO backup_role;

            ALTER TABLE import_batch_errors ENABLE ROW LEVEL SECURITY;
            ALTER TABLE import_batch_errors FORCE ROW LEVEL SECURITY;

            CREATE POLICY import_batch_errors_isolation ON import_batch_errors
                FOR ALL
                TO app
                USING (
                    tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid
                )
                WITH CHECK (
                    tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid
                );

            GRANT SELECT, INSERT, UPDATE, DELETE ON import_batch_errors TO app;
            GRANT SELECT ON import_batch_errors TO backup_role;
        """))


def downgrade() -> None:
    conn = op.get_bind()

    if conn.dialect.name == "postgresql":
        conn.execute(sa.text("""
            DROP POLICY IF EXISTS tenant_import_mappings_isolation ON tenant_import_mappings;
            DROP POLICY IF EXISTS import_batch_errors_isolation ON import_batch_errors;
        """))

    op.drop_table("import_batch_errors")
    op.drop_table("tenant_import_mappings")
