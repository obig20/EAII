"""
tests/kernel/test_retailer_confirmation.py

Tests for retailer balance confirmations via signed expiring links (Feature D).
Enforces:
  - Non-negotiable Rule 2:
      * Confirming a balance does NOT mark any payment confirmed unless the retailer explicitly lists that payment.
      * Explicit payment information provided by buyer creates Evidence Level L3.
  - Non-negotiable Rule 4: Append-only audit logging for confirmations and disputes.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.kernel.enums import EvidenceLevel, InvoiceState, PaymentState
from app.kernel.models import Event, Invoice, PaymentLine, Retailer, RetailerConfirmationToken, Tenant, User
from app.kernel.retailer_confirmation import (
    DisputeReasonCode,
    TokenAlreadyUsedError,
    TokenExpiredError,
    TokenNotFoundError,
    confirm_retailer_balance,
    create_retailer_confirmation_token,
    dispute_retailer_balance,
    hash_token,
    validate_confirmation_token,
)


@pytest.fixture
def retailer_test(db_session: Session, tenant_a: Tenant) -> Retailer:
    r = Retailer(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        name="Merkato Supermarket",
        phone="+251911445566",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(r)
    db_session.flush()
    return r


@pytest.fixture
def open_invoices(
    db_session: Session,
    tenant_a: Tenant,
    owner_user_a: User,
    retailer_test: Retailer,
) -> list[Invoice]:
    now = datetime.now(timezone.utc)
    inv1 = Invoice(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        retailer_id=retailer_test.id,
        invoice_number="INV-CONF-1",
        amount_santim=250000,  # 2,500.00 ETB
        issued_date=date(2026, 10, 1),
        due_date=date(2026, 10, 31),
        state=InvoiceState.OPEN.value,
        created_at=now,
        created_by=owner_user_a.id,
    )
    inv2 = Invoice(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        retailer_id=retailer_test.id,
        invoice_number="INV-CONF-2",
        amount_santim=400000,  # 4,000.00 ETB
        issued_date=date(2026, 10, 5),
        due_date=date(2026, 11, 5),
        state=InvoiceState.OPEN.value,
        created_at=now,
        created_by=owner_user_a.id,
    )
    db_session.add_all([inv1, inv2])
    db_session.flush()
    return [inv1, inv2]


class TestConfirmationTokenManagement:
    def test_create_and_validate_token_success(
        self,
        db_session: Session,
        tenant_a: Tenant,
        retailer_test: Retailer,
        open_invoices: list[Invoice],
    ):
        token_rec, raw_token = create_retailer_confirmation_token(
            db_session,
            tenant_id=tenant_a.id,
            retailer_id=retailer_test.id,
            expires_in_days=7,
        )

        assert token_rec.id is not None
        assert token_rec.token_hash == hash_token(raw_token)
        assert token_rec.used_at is None
        assert token_rec.expires_at > datetime.now(timezone.utc)

        # Validate token
        rec, ret, invs = validate_confirmation_token(db_session, raw_token=raw_token)
        assert rec.id == token_rec.id
        assert ret.id == retailer_test.id
        assert len(invs) == 2

    def test_validate_nonexistent_token_raises(self, db_session: Session):
        with pytest.raises(TokenNotFoundError, match="Invalid confirmation link"):
            validate_confirmation_token(db_session, raw_token="non-existent-fake-token")

    def test_validate_expired_token_raises(
        self,
        db_session: Session,
        tenant_a: Tenant,
        retailer_test: Retailer,
    ):
        token_rec, raw_token = create_retailer_confirmation_token(
            db_session,
            tenant_id=tenant_a.id,
            retailer_id=retailer_test.id,
            expires_in_days=-1,  # Expired yesterday
        )

        with pytest.raises(TokenExpiredError, match="expired"):
            validate_confirmation_token(db_session, raw_token=raw_token)

    def test_validate_already_used_token_raises(
        self,
        db_session: Session,
        tenant_a: Tenant,
        retailer_test: Retailer,
    ):
        token_rec, raw_token = create_retailer_confirmation_token(
            db_session,
            tenant_id=tenant_a.id,
            retailer_id=retailer_test.id,
            expires_in_days=7,
        )
        token_rec.used_at = datetime.now(timezone.utc)
        db_session.flush()

        with pytest.raises(TokenAlreadyUsedError, match="already completed"):
            validate_confirmation_token(db_session, raw_token=raw_token)


class TestConfirmRetailerBalance:
    def test_confirm_balance_without_explicit_payment_does_not_confirm_payment(
        self,
        db_session: Session,
        tenant_a: Tenant,
        retailer_test: Retailer,
        open_invoices: list[Invoice],
    ):
        """
        NON-NEGOTIABLE RULE 2:
        Confirming a balance does NOT mark any payment confirmed unless the retailer
        explicitly lists that payment.
        """
        token_rec, raw_token = create_retailer_confirmation_token(
            db_session,
            tenant_id=tenant_a.id,
            retailer_id=retailer_test.id,
        )

        ok, l3_payments = confirm_retailer_balance(db_session, token_record=token_rec)
        assert ok is True
        assert len(l3_payments) == 0

        # Invoices MUST REMAIN OPEN!
        for inv in open_invoices:
            db_session.refresh(inv)
            assert inv.state == InvoiceState.OPEN.value

        # Event logged
        events = db_session.execute(
            select(Event).where(
                Event.tenant_id == tenant_a.id,
                Event.event_type == "retailer.balance_confirmed",
            )
        ).scalars().all()
        assert len(events) == 1

    def test_confirm_balance_with_explicit_payment_creates_l3(
        self,
        db_session: Session,
        tenant_a: Tenant,
        retailer_test: Retailer,
        open_invoices: list[Invoice],
    ):
        """
        Buyer's confirmation with explicit details qualifies as Evidence Level L3!
        """
        token_rec, raw_token = create_retailer_confirmation_token(
            db_session,
            tenant_id=tenant_a.id,
            retailer_id=retailer_test.id,
        )

        explicit = [{
            "amount_etb": "2500.00",
            "source_ref": "FT-CBE-RETAILER-001",
            "value_date": date(2026, 10, 20),
            "raw_reference": "Paid at Merkato Branch",
        }]

        ok, l3_payments = confirm_retailer_balance(
            db_session,
            token_record=token_rec,
            explicit_payments=explicit,
        )

        assert ok is True
        assert len(l3_payments) == 1
        pl = l3_payments[0]
        assert pl.evidence_level == EvidenceLevel.L3.value
        assert pl.state == PaymentState.CONFIRMED.value
        assert pl.amount_santim == 250000


class TestDisputeRetailerBalance:
    def test_dispute_balance_requires_explanation(
        self,
        db_session: Session,
        tenant_a: Tenant,
        retailer_test: Retailer,
        open_invoices: list[Invoice],
    ):
        token_rec, _ = create_retailer_confirmation_token(
            db_session,
            tenant_id=tenant_a.id,
            retailer_id=retailer_test.id,
        )

        with pytest.raises(ValueError, match="explanation is required"):
            dispute_retailer_balance(
                db_session,
                token_record=token_rec,
                reason_code=DisputeReasonCode.WRONG_AMOUNT.value,
                explanation="",
            )

    def test_dispute_balance_sets_invoices_to_disputed_and_logs_event(
        self,
        db_session: Session,
        tenant_a: Tenant,
        retailer_test: Retailer,
        open_invoices: list[Invoice],
    ):
        token_rec, _ = create_retailer_confirmation_token(
            db_session,
            tenant_id=tenant_a.id,
            retailer_id=retailer_test.id,
        )

        disputed = dispute_retailer_balance(
            db_session,
            token_record=token_rec,
            reason_code=DisputeReasonCode.GOODS_RETURNED.value,
            explanation="Part of goods were returned due to packaging defect.",
        )

        assert len(disputed) == 2
        for inv in open_invoices:
            db_session.refresh(inv)
            assert inv.state == InvoiceState.DISPUTED.value

        # Check append-only event
        events = db_session.execute(
            select(Event).where(
                Event.tenant_id == tenant_a.id,
                Event.event_type == "retailer.balance_disputed",
            )
        ).scalars().all()
        assert len(events) == 1
        assert events[0].payload["reason_code"] == DisputeReasonCode.GOODS_RETURNED.value
