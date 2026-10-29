"""
0005_stage1_review_safety

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-27 12:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Add is_previously_reversed flag to payment_lines
    op.add_column(
        "payment_lines",
        sa.Column(
            "is_previously_reversed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # 2. Create reversal_notes table for free-text notes (separate from immutable events)
    op.create_table(
        "reversal_notes",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column(
            "payment_match_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("payment_matches.id"),
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
        "ix_reversal_notes_tenant_id", "reversal_notes", ["tenant_id"]
    )
    op.create_index(
        "ix_reversal_notes_match_id", "reversal_notes", ["payment_match_id"]
    )

    # 3. Create processed_approval_tokens table for idempotent retry
    op.create_table(
        "processed_approval_tokens",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column(
            "actor_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "payment_match_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("payment_matches.id"),
            nullable=False,
        ),
        sa.Column("result_status", sa.String(32), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_index(
        "ix_processed_approval_tokens_tenant_id", "processed_approval_tokens", ["tenant_id"]
    )
    op.create_index(
        "ix_processed_approval_tokens_token_hash", "processed_approval_tokens", ["token_hash"]
    )
    op.create_index(
        "ix_processed_approval_tokens_match_id", "processed_approval_tokens", ["payment_match_id"]
    )

    # 4. RLS Policies and Permissions
    conn.execute(sa.text("""
        ALTER TABLE reversal_notes ENABLE ROW LEVEL SECURITY;
        ALTER TABLE reversal_notes FORCE ROW LEVEL SECURITY;

        CREATE POLICY reversal_notes_tenant_isolation ON reversal_notes
            FOR ALL
            TO app
            USING (
                tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid
            )
            WITH CHECK (
                tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid
            );

        GRANT SELECT, INSERT, UPDATE, DELETE ON reversal_notes TO app;
        GRANT SELECT ON reversal_notes TO backup_role;

        ALTER TABLE processed_approval_tokens ENABLE ROW LEVEL SECURITY;
        ALTER TABLE processed_approval_tokens FORCE ROW LEVEL SECURITY;

        CREATE POLICY processed_approval_tokens_tenant_isolation ON processed_approval_tokens
            FOR ALL
            TO app
            USING (
                tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid
            )
            WITH CHECK (
                tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid
            );

        GRANT SELECT, INSERT, UPDATE, DELETE ON processed_approval_tokens TO app;
        GRANT SELECT ON processed_approval_tokens TO backup_role;

        CREATE OR REPLACE FUNCTION check_no_auto_confirm_on_reversed()
        RETURNS TRIGGER AS $$
        DECLARE
            v_is_reversed BOOLEAN;
        BEGIN
            IF NEW.event_type = 'payment.auto_confirmed' AND NEW.aggregate_type = 'payment_line' THEN
                SELECT is_previously_reversed INTO v_is_reversed
                FROM payment_lines
                WHERE id = NEW.aggregate_id;
                
                IF v_is_reversed IS TRUE THEN
                    RAISE EXCEPTION 'Database invariant violation: payment_line % is previously reversed and cannot be auto-confirmed', NEW.aggregate_id;
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        DROP TRIGGER IF EXISTS trg_check_no_auto_confirm_on_reversed ON events;
        CREATE TRIGGER trg_check_no_auto_confirm_on_reversed
        BEFORE INSERT ON events
        FOR EACH ROW
        EXECUTE FUNCTION check_no_auto_confirm_on_reversed();
    """))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("""
        DROP TRIGGER IF EXISTS trg_check_no_auto_confirm_on_reversed ON events;
        DROP FUNCTION IF EXISTS check_no_auto_confirm_on_reversed();
        DROP POLICY IF EXISTS processed_approval_tokens_tenant_isolation ON processed_approval_tokens;
        DROP POLICY IF EXISTS reversal_notes_tenant_isolation ON reversal_notes;
    """))
    op.drop_index("ix_processed_approval_tokens_match_id", table_name="processed_approval_tokens")
    op.drop_index("ix_processed_approval_tokens_token_hash", table_name="processed_approval_tokens")
    op.drop_index("ix_processed_approval_tokens_tenant_id", table_name="processed_approval_tokens")
    op.drop_table("processed_approval_tokens")
    op.drop_index("ix_reversal_notes_match_id", table_name="reversal_notes")
    op.drop_index("ix_reversal_notes_tenant_id", table_name="reversal_notes")
    op.drop_table("reversal_notes")
    op.drop_column("payment_lines", "is_previously_reversed")
