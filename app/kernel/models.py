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
from datetime import date, datetime

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
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
    # telegram_id stored hashed in Stage 4; kept nullable here
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )

    tenant: Mapped["Tenant"] = relationship(back_populates="users")


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
        # Rule: UNIQUE invoice_number per tenant
        UniqueConstraint("tenant_id", "invoice_number", name="uq_invoice_number_per_tenant"),
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
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    source_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
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
