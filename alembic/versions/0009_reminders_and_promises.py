"""
0009_reminders_and_promises

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-12 13:40:00.000000
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

    # 1. Create promises_to_pay table
    op.create_table(
        "promises_to_pay",
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
        sa.Column("amount_santim", sa.BigInteger(), nullable=False),
        sa.Column("promised_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
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
        sa.Column("fulfilled_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "fulfilled_payment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("payment_lines.id"),
            nullable=True,
        ),
        sa.CheckConstraint("amount_santim > 0", name="chk_promise_amount_positive"),
    )

    op.create_index(
        "ix_promises_to_pay_tenant_id",
        "promises_to_pay",
        ["tenant_id"],
    )
    op.create_index(
        "ix_promises_to_pay_retailer_id",
        "promises_to_pay",
        ["tenant_id", "retailer_id"],
    )
    op.create_index(
        "ix_promises_to_pay_date",
        "promises_to_pay",
        ["tenant_id", "promised_date"],
    )
    op.create_index(
        "ix_promises_to_pay_status",
        "promises_to_pay",
        ["tenant_id", "status"],
    )

    # 2. RLS Policies and Permissions
    if conn.dialect.name == "postgresql":
        conn.execute(sa.text("""
            ALTER TABLE promises_to_pay ENABLE ROW LEVEL SECURITY;
            ALTER TABLE promises_to_pay FORCE ROW LEVEL SECURITY;

            CREATE POLICY promises_to_pay_isolation ON promises_to_pay
                FOR ALL
                TO app
                USING (
                    tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid
                )
                WITH CHECK (
                    tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid
                );

            GRANT SELECT, INSERT, UPDATE, DELETE ON promises_to_pay TO app;
            GRANT SELECT ON promises_to_pay TO backup_role;
        """))


def downgrade() -> None:
    conn = op.get_bind()

    if conn.dialect.name == "postgresql":
        conn.execute(sa.text("""
            DROP POLICY IF EXISTS promises_to_pay_isolation ON promises_to_pay;
        """))

    op.drop_table("promises_to_pay")
