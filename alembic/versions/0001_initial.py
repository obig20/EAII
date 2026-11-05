"""0001_initial.py

Initial migration: all domain tables, constraints, RLS policies, and privilege
revocations for the append-only event log.

Up actions:
  1. Create all tables in dependency order.
  2. Add CHECK constraints on monetary columns.
  3. Enable and FORCE Row-Level Security on every tenant-scoped table.
  4. Create tenant_isolation policies.
  5. REVOKE UPDATE and DELETE on events from the app role.

Down actions: drop everything in reverse order.

Revision ID: 0001
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# ---------------------------------------------------------------------------
# Tenant-scoped tables (RLS applied to all of these)
# ---------------------------------------------------------------------------
TENANT_SCOPED_TABLES = [
    "users",
    "retailers",
    "invoices",
    "payment_lines",
    "import_batches",
    "payment_matches",
    "events",
    "retailer_confirmation_tokens",
]


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. tenants (no RLS — must be readable to look up tenant)
    # ------------------------------------------------------------------
    op.create_table(
        "tenants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )

    # ------------------------------------------------------------------
    # 2. users
    # ------------------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("display_name", sa.Text, nullable=False),
        sa.Column("telegram_id", sa.BigInteger, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"])

    # ------------------------------------------------------------------
    # 3. retailers
    # ------------------------------------------------------------------
    op.create_table(
        "retailers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("phone", sa.Text, nullable=True),
        sa.Column("bank_account", sa.Text, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_retailers_tenant_id", "retailers", ["tenant_id"])

    # ------------------------------------------------------------------
    # 4. import_batches
    # ------------------------------------------------------------------
    op.create_table(
        "import_batches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("source_type", sa.Text, nullable=False),
        sa.Column("evidence_level", sa.String(2), nullable=False),
        sa.Column("filename", sa.Text, nullable=True),
        sa.Column("imported_by", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id"), nullable=False),
        sa.Column("imported_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_import_batches_tenant_id", "import_batches", ["tenant_id"])

    # ------------------------------------------------------------------
    # 5. invoices
    # ------------------------------------------------------------------
    op.create_table(
        "invoices",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("invoice_number", sa.Text, nullable=False),
        sa.Column("retailer_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("retailers.id"), nullable=False),
        sa.Column("amount_santim", sa.BigInteger, nullable=False),
        sa.Column("issued_date", sa.Date, nullable=False),
        sa.Column("due_date", sa.Date, nullable=False),
        sa.Column("state", sa.String(20), nullable=False, server_default="open"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("created_by", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id"), nullable=False),
        sa.CheckConstraint("amount_santim > 0", name="ck_invoice_amount_positive"),
        sa.UniqueConstraint("tenant_id", "invoice_number",
                            name="uq_invoice_number_per_tenant"),
    )
    op.create_index("ix_invoices_tenant_id", "invoices", ["tenant_id"])

    # ------------------------------------------------------------------
    # 6. payment_lines
    # ------------------------------------------------------------------
    op.create_table(
        "payment_lines",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("source_ref", sa.Text, nullable=True),
        sa.Column("amount_santim", sa.BigInteger, nullable=False),
        sa.Column("value_date", sa.Date, nullable=False),
        sa.Column("counterparty_name", sa.Text, nullable=True),
        sa.Column("counterparty_account", sa.Text, nullable=True),
        sa.Column("raw_reference", sa.Text, nullable=True),
        sa.Column("evidence_level", sa.String(2), nullable=False),
        sa.Column("state", sa.String(25), nullable=False, server_default="unmatched"),
        sa.Column("import_batch_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("import_batches.id"), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint("amount_santim > 0", name="ck_payment_line_amount_positive"),
    )
    op.create_index("ix_payment_lines_tenant_id", "payment_lines", ["tenant_id"])

    # ------------------------------------------------------------------
    # 7. payment_matches
    # ------------------------------------------------------------------
    op.create_table(
        "payment_matches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("payment_line_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("payment_lines.id"), nullable=False),
        sa.Column("invoice_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("invoices.id"), nullable=False),
        sa.Column("matched_santim", sa.BigInteger, nullable=False),
        sa.Column("tier", sa.String(10), nullable=False),
        sa.Column("state", sa.String(20), nullable=False, server_default="proposed"),
        sa.Column("confirmed_by", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id"), nullable=True),
        sa.Column("confirmed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint("matched_santim > 0", name="ck_match_amount_positive"),
    )
    op.create_index("ix_payment_matches_tenant_id", "payment_matches", ["tenant_id"])

    # ------------------------------------------------------------------
    # 8. events (append-only)
    # ------------------------------------------------------------------
    op.create_table(
        "events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("aggregate_type", sa.Text, nullable=False),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("evidence_level", sa.String(2), nullable=True),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_events_tenant_id", "events", ["tenant_id"])
    op.create_index("ix_events_aggregate_id", "events", ["aggregate_id"])

    # ------------------------------------------------------------------
    # 9. retailer_confirmation_tokens
    # ------------------------------------------------------------------
    op.create_table(
        "retailer_confirmation_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("retailer_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("retailers.id"), nullable=False),
        sa.Column("token_hash", sa.Text, nullable=False, unique=True),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("used_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_retailer_confirmation_tokens_tenant_id",
        "retailer_confirmation_tokens",
        ["tenant_id"],
    )

    # ------------------------------------------------------------------
    # 10. Row-Level Security
    # ------------------------------------------------------------------
    conn = op.get_bind()

    for table in TENANT_SCOPED_TABLES:
        conn.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        conn.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
        conn.execute(sa.text(f"""
            CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id = NULLIF(current_setting('app.current_tenant', TRUE), '')::uuid)
        """))

    # ------------------------------------------------------------------
    # 11. Grant permissions to app role, then Revoke UPDATE and DELETE on events
    #     (enforce append-only at DB privilege level)
    # ------------------------------------------------------------------
    conn.execute(sa.text("""
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'app') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app;
                GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app;
                REVOKE UPDATE, DELETE ON events FROM app;
            END IF;
        END
        $$;
    """))


def downgrade() -> None:
    conn = op.get_bind()

    # Re-grant before dropping (in case privileges matter for future re-runs)
    try:
        conn.execute(sa.text("GRANT UPDATE, DELETE ON events TO app"))
    except Exception:
        pass

    # Drop tables in reverse dependency order
    tables_to_drop = [
        "retailer_confirmation_tokens",
        "events",
        "payment_matches",
        "payment_lines",
        "import_batches",
        "invoices",
        "retailers",
        "users",
        "tenants",
    ]
    for table in tables_to_drop:
        op.drop_table(table)
