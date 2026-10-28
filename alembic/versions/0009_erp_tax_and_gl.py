"""
0009_erp_tax_and_gl

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-28 09:00:00.000000

Adds ERP Ethiopian Tax Compliance and Double-Entry General Ledger tables:
- withholding_receipts
- accounts
- journal_entries
- journal_lines

Enforces Row-Level Security (RLS) on all tables for app role.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # 1. withholding_receipts
    op.create_table(
        "withholding_receipts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("receipt_number", sa.String(64), nullable=False),
        sa.Column("counterparty_type", sa.String(16), nullable=False),
        sa.Column("counterparty_name", sa.String(128), nullable=False),
        sa.Column("counterparty_tin", sa.String(32), nullable=False),
        sa.Column("source_invoice_ref", sa.String(64), nullable=False),
        sa.Column("gross_amount_santim", sa.BigInteger(), nullable=False),
        sa.Column("withheld_amount_santim", sa.BigInteger(), nullable=False),
        sa.Column("issued_date", sa.Date(), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant_id", "receipt_number", name="uq_withholding_tenant_receipt_num"),
        sa.CheckConstraint("gross_amount_santim > 0", name="ck_wht_gross_positive"),
        sa.CheckConstraint("withheld_amount_santim > 0", name="ck_wht_withheld_positive"),
    )
    op.create_index("ix_withholding_receipts_tenant_id", "withholding_receipts", ["tenant_id"])

    # 2. accounts
    op.create_table(
        "accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("account_code", sa.String(32), nullable=False),
        sa.Column("account_name", sa.String(128), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant_id", "account_code", name="uq_accounts_tenant_code"),
    )
    op.create_index("ix_accounts_tenant_id", "accounts", ["tenant_id"])

    # 3. journal_entries
    op.create_table(
        "journal_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("entry_number", sa.String(64), nullable=False),
        sa.Column("entry_date", sa.Date(), nullable=False),
        sa.Column("reference", sa.String(64), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False, server_default=sa.text("'posted'")),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant_id", "entry_number", name="uq_journal_entries_tenant_num"),
    )
    op.create_index("ix_journal_entries_tenant_id", "journal_entries", ["tenant_id"])

    # 4. journal_lines
    op.create_table(
        "journal_lines",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("journal_entry_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("journal_entries.id"), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("debit_santim", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("credit_santim", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("description", sa.String(255), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("debit_santim >= 0", name="ck_jl_debit_non_negative"),
        sa.CheckConstraint("credit_santim >= 0", name="ck_jl_credit_non_negative"),
    )
    op.create_index("ix_journal_lines_tenant_id", "journal_lines", ["tenant_id"])
    op.create_index("ix_journal_lines_je_id", "journal_lines", ["journal_entry_id"])
    op.create_index("ix_journal_lines_account_id", "journal_lines", ["account_id"])

    # 5. Enable and Force Row-Level Security
    tables = [
        "withholding_receipts",
        "accounts",
        "journal_entries",
        "journal_lines",
    ]

    for tbl in tables:
        conn.execute(sa.text(f"""
            ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE {tbl} FORCE ROW LEVEL SECURITY;

            CREATE POLICY {tbl}_tenant_isolation ON {tbl}
                FOR ALL
                TO app
                USING (
                    tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid
                )
                WITH CHECK (
                    tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid
                );

            GRANT SELECT, INSERT, UPDATE, DELETE ON {tbl} TO app;
            GRANT SELECT ON {tbl} TO backup_role;
        """))


def downgrade() -> None:
    tables = [
        "journal_lines",
        "journal_entries",
        "accounts",
        "withholding_receipts",
    ]
    for tbl in tables:
        op.drop_table(tbl)
