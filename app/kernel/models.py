"""
app/kernel/models.py

SQLAlchemy 2.x mapped classes for every domain table.

Key invariants (enforced by DB constraints AND application guards):
  - All monetary columns are BIGINT (santim).  Float is NEVER used.
  - Every tenant-scoped table has tenant_id with RLS applied by migration.
  - invoice_number is UNIQUE per tenant.
  - events has no UPDATE or DELETE grants (enforced by migration REVOKE).
  - UUIDs are generated server-side via gen_random_uuid().

Do NOT import FastAPI or aiogram here.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------
class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Helper: server-side UUID default (string form for SA compatibility)
# ---------------------------------------------------------------------------
def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=None,  # let Python generate; migration uses gen_random_uuid()
    )


# ---------------------------------------------------------------------------
# tenants
# ---------------------------------------------------------------------------
class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    is_demo: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )

    # relationships
    users: Mapped[list["User"]] = relationship(back_populates="tenant")
    retailers: Mapped[list["Retailer"]] = relationship(back_populates="tenant")
    invoices: Mapped[list["Invoice"]] = relationship(back_populates="tenant")


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(
        String(20), nullable=False  # 'owner' | 'cashier'
    )
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    failed_login_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    # telegram_id stored hashed in Stage 4; kept nullable here
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )

    tenant: Mapped["Tenant"] = relationship(back_populates="users")
    sessions: Mapped[list["Session"]] = relationship(back_populates="user", cascade="all, delete-orphan")


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------
class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    last_activity_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(Text, nullable=True)

    tenant: Mapped["Tenant"] = relationship()
    user: Mapped["User"] = relationship(back_populates="sessions")


# ---------------------------------------------------------------------------
# rate_limit_buckets (persistent rate limiting & lockout)
# ---------------------------------------------------------------------------
class RateLimitBucket(Base):
    __tablename__ = "rate_limit_buckets"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_attempt_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    last_attempt_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    locked_until: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )


# ---------------------------------------------------------------------------
# auth_failures (un-tenanted persistent failed auth log, SEC-12, SEC-06)
# ---------------------------------------------------------------------------
class AuthFailure(Base):
    __tablename__ = "auth_failures"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    identifier_hmac: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    ip_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    failed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True
    )


# ---------------------------------------------------------------------------
# telegram_invites
# ---------------------------------------------------------------------------
class TelegramInvite(Base):
    __tablename__ = "telegram_invites"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    code_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    used_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    used_by_telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    tenant: Mapped["Tenant"] = relationship()


# ---------------------------------------------------------------------------
# retailers
# ---------------------------------------------------------------------------
class Retailer(Base):
    __tablename__ = "retailers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    phone: Mapped[str | None] = mapped_column(Text, nullable=True)
    bank_account: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )

    tenant: Mapped["Tenant"] = relationship(back_populates="retailers")
    invoices: Mapped[list["Invoice"]] = relationship(back_populates="retailer")
    confirmation_tokens: Mapped[list["RetailerConfirmationToken"]] = relationship(
        back_populates="retailer"
    )


# ---------------------------------------------------------------------------
# import_batches
# ---------------------------------------------------------------------------
class ImportBatch(Base):
    __tablename__ = "import_batches"
    __table_args__ = (
        UniqueConstraint("tenant_id", "file_hash", name="uq_import_batches_tenant_file_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_level: Mapped[str] = mapped_column(
        String(2), nullable=False  # 'L1' | 'L2' | 'L3' | 'L4'
    )
    filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    imported_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    imported_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )

    payment_lines: Mapped[list["PaymentLine"]] = relationship(
        back_populates="import_batch"
    )


# ---------------------------------------------------------------------------
# invoices
# ---------------------------------------------------------------------------
class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (
        Index(
            "uq_active_invoice_number_per_tenant",
            "tenant_id",
            "invoice_number",
            unique=True,
            postgresql_where=text("state != 'void'"),
        ),
        # Rule: amount must be positive integer santim
        CheckConstraint("amount_santim > 0", name="ck_invoice_amount_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    invoice_number: Mapped[str] = mapped_column(Text, nullable=False)
    retailer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("retailers.id"), nullable=False
    )
    # BIGINT — santim only.  Never float.
    amount_santim: Mapped[int] = mapped_column(BigInteger, nullable=False)
    issued_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, default="open"
    )
    replaces_invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )

    tenant: Mapped["Tenant"] = relationship(back_populates="invoices")
    retailer: Mapped["Retailer"] = relationship(back_populates="invoices")
    payment_matches: Mapped[list["PaymentMatch"]] = relationship(
        back_populates="invoice"
    )


# ---------------------------------------------------------------------------
# payment_lines
# ---------------------------------------------------------------------------
class PaymentLine(Base):
    __tablename__ = "payment_lines"
    __table_args__ = (
        CheckConstraint("amount_santim > 0", name="ck_payment_line_amount_positive"),
        UniqueConstraint("tenant_id", "row_key", name="uq_payment_lines_tenant_row_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    source_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    row_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    amount_santim: Mapped[int] = mapped_column(BigInteger, nullable=False)
    value_date: Mapped[date] = mapped_column(Date, nullable=False)
    counterparty_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    counterparty_account: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_level: Mapped[str] = mapped_column(
        String(2), nullable=False  # 'L1' | 'L2' | 'L3' | 'L4'
    )
    state: Mapped[str] = mapped_column(
        String(25), nullable=False, default="unmatched"
    )
    is_previously_reversed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    import_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("import_batches.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )

    import_batch: Mapped["ImportBatch | None"] = relationship(
        back_populates="payment_lines"
    )
    payment_matches: Mapped[list["PaymentMatch"]] = relationship(
        back_populates="payment_line"
    )


# ---------------------------------------------------------------------------
# payment_matches
# ---------------------------------------------------------------------------
class PaymentMatch(Base):
    __tablename__ = "payment_matches"
    __table_args__ = (
        CheckConstraint("matched_santim > 0", name="ck_match_amount_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    payment_line_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_lines.id"), nullable=False
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id"), nullable=False
    )
    # The portion of this payment applied to this invoice (santim)
    matched_santim: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tier: Mapped[str] = mapped_column(
        String(10), nullable=False  # 'tier_a' | 'tier_b' | 'tier_c'
    )
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, default="proposed"
    )
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )

    payment_line: Mapped["PaymentLine"] = relationship(back_populates="payment_matches")
    invoice: Mapped["Invoice"] = relationship(back_populates="payment_matches")


# ---------------------------------------------------------------------------
# events (append-only)
# ---------------------------------------------------------------------------
class Event(Base):
    __tablename__ = "events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_type: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    evidence_level: Mapped[str | None] = mapped_column(String(2), nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )


# ---------------------------------------------------------------------------
# retailer_confirmation_tokens  (stub — fully used in Stage 4)
# ---------------------------------------------------------------------------
class RetailerConfirmationToken(Base):
    __tablename__ = "retailer_confirmation_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    retailer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("retailers.id"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    used_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    retailer: Mapped["Retailer"] = relationship(back_populates="confirmation_tokens")


# ---------------------------------------------------------------------------
# reversal_notes (deletable notes table, separate from immutable events)
# ---------------------------------------------------------------------------
class ReversalNote(Base):
    __tablename__ = "reversal_notes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    payment_match_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_matches.id"), nullable=False, index=True
    )
    reason_code: Mapped[str] = mapped_column(String(32), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )


# ---------------------------------------------------------------------------
# processed_approval_tokens (idempotent retry tracking table)
# ---------------------------------------------------------------------------
class ProcessedApprovalToken(Base):
    __tablename__ = "processed_approval_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    actor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    payment_match_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_matches.id"), nullable=False, index=True
    )
    result_status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )



# ---------------------------------------------------------------------------
# invoice_void_notes (deletable notes table for voided sales)
# ---------------------------------------------------------------------------
class InvoiceVoidNote(Base):
    __tablename__ = "invoice_void_notes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id"), nullable=False, index=True
    )
    reason_code: Mapped[str] = mapped_column(String(32), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )


# ---------------------------------------------------------------------------
# tenant_import_mappings (saved statement format mappings per tenant)
# ---------------------------------------------------------------------------
class TenantImportMapping(Base):
    __tablename__ = "tenant_import_mappings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_name: Mapped[str] = mapped_column(String(100), nullable=False)
    delimiter: Mapped[str] = mapped_column(String(5), nullable=False, default=",")
    encoding: Mapped[str] = mapped_column(String(20), nullable=False, default="utf-8")
    header_row_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    date_column: Mapped[str] = mapped_column(String(100), nullable=False)
    date_format: Mapped[str] = mapped_column(String(50), nullable=False, default="%Y-%m-%d")
    amount_column: Mapped[str | None] = mapped_column(String(100), nullable=True)
    credit_column: Mapped[str | None] = mapped_column(String(100), nullable=True)
    debit_column: Mapped[str | None] = mapped_column(String(100), nullable=True)
    reference_column: Mapped[str | None] = mapped_column(String(100), nullable=True)
    narrative_column: Mapped[str | None] = mapped_column(String(100), nullable=True)
    counterparty_name_column: Mapped[str | None] = mapped_column(String(100), nullable=True)
    counterparty_account_column: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tx_id_column: Mapped[str | None] = mapped_column(String(100), nullable=True)
    number_format: Mapped[str] = mapped_column(String(20), nullable=False, default="1,234.56")
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )


# ---------------------------------------------------------------------------
# import_batch_errors (row-level parse/validation errors per batch)
# ---------------------------------------------------------------------------
class ImportBatchError(Base):
    __tablename__ = "import_batch_errors"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    import_batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("import_batches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    row_index: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_data: Mapped[str] = mapped_column(Text, nullable=False)
    error_reason: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )


# ---------------------------------------------------------------------------
# promises_to_pay (commitments from retailers to pay outstanding invoices)
# ---------------------------------------------------------------------------
class PromiseToPay(Base):
    __tablename__ = "promises_to_pay"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    retailer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("retailers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    amount_santim: Mapped[int] = mapped_column(BigInteger, nullable=False)
    promised_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    fulfilled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    fulfilled_payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_lines.id"), nullable=True
    )


# ---------------------------------------------------------------------------
# pilot_snapshots (point-in-time baseline and audit snapshots)
# ---------------------------------------------------------------------------
class PilotSnapshot(Base):
    __tablename__ = "pilot_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    snapshot_type: Mapped[str] = mapped_column(String(50), nullable=False, default="baseline")
    total_credit_sales_santim: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    total_overdue_santim: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    invoice_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retailer_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    metrics_payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


# ---------------------------------------------------------------------------
# payer_aliases (third-party payment memory per retailer)
# ---------------------------------------------------------------------------
class PayerAlias(Base):
    __tablename__ = "payer_aliases"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    retailer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("retailers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    normalized_payer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    normalized_payer_ref: Mapped[str | None] = mapped_column(String(100), nullable=True)
    raw_payer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    raw_payer_ref: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    retailer: Mapped["Retailer"] = relationship("Retailer")
    creator: Mapped["User"] = relationship("User")
