"""
0007_void_sales

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-09 14:48:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Add replaces_invoice_id column to invoices
    op.add_column(
        "invoices",
        sa.Column(
            "replaces_invoice_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("invoices.id"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_invoices_replaces_invoice_id",
        "invoices",
        ["tenant_id", "replaces_invoice_id"],
    )

    # 2. Replace absolute unique constraint with partial unique index
    # allowing voided invoice numbers to be reused via void-and-reenter flow
    op.drop_constraint("uq_invoice_number_per_tenant", "invoices", type_="unique")
    op.create_index(
        "uq_active_invoice_number_per_tenant",
        "invoices",
        ["tenant_id", "invoice_number"],
        unique=True,
        postgresql_where=sa.text("state != 'void'"),
    )

    # 3. Create invoice_void_notes table for free-text notes (separate from immutable events)
    op.create_table(
        "invoice_void_notes",
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
            "invoice_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("invoices.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("reason_code", sa.String(32), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
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
    )

    op.create_index(
        "ix_invoice_void_notes_tenant_id", "invoice_void_notes", ["tenant_id"]
    )
    op.create_index(
        "ix_invoice_void_notes_invoice_id", "invoice_void_notes", ["invoice_id"]
    )

    # 4. RLS Policies and Permissions on invoice_void_notes
    if conn.dialect.name == "postgresql":
        conn.execute(sa.text("""
            ALTER TABLE invoice_void_notes ENABLE ROW LEVEL SECURITY;
            ALTER TABLE invoice_void_notes FORCE ROW LEVEL SECURITY;

            CREATE POLICY invoice_void_notes_tenant_isolation ON invoice_void_notes
                FOR ALL
                TO app
                USING (
                    tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid
                )
                WITH CHECK (
                    tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid
                );

            GRANT SELECT, INSERT, UPDATE, DELETE ON invoice_void_notes TO app;
            GRANT SELECT ON invoice_void_notes TO backup_role;
        """))


def downgrade() -> None:
    conn = op.get_bind()

    if conn.dialect.name == "postgresql":
        conn.execute(sa.text("""
            DROP POLICY IF EXISTS invoice_void_notes_tenant_isolation ON invoice_void_notes;
        """))

    op.drop_table("invoice_void_notes")
    op.drop_index("uq_active_invoice_number_per_tenant", table_name="invoices")
    op.create_unique_constraint(
        "uq_invoice_number_per_tenant", "invoices", ["tenant_id", "invoice_number"]
    )
    op.drop_index("ix_invoices_replaces_invoice_id", table_name="invoices")
    op.drop_column("invoices", "replaces_invoice_id")
