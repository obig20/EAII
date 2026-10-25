"""
tests/kernel/test_sales.py

Tests for credit-sale capture domain logic (Rule 1, Rule 4, Rule 5, sequence gaps).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.kernel.enums import InvoiceState
from app.kernel.models import Event, Invoice, Retailer, Tenant, User
from app.kernel.sales import (
    DuplicateInvoiceError,
    InvalidSaleError,
    detect_sequence_gaps,
    parse_and_record_sales_csv,
    record_credit_sale,
)


@pytest.fixture
def retailer_a(db_session: Session, tenant_a: Tenant) -> Retailer:
    ret = Retailer(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        name="Abebe General Trading",
        phone="+251911223344",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(ret)
    db_session.flush()
    return ret


class TestDetectSequenceGaps:
    def test_empty_and_single(self):
        assert detect_sequence_gaps([]) == []
        assert detect_sequence_gaps(["INV-001"]) == []

    def test_continuous_sequence(self):
        numbers = ["INV-001", "INV-002", "INV-003", "INV-004"]
        assert detect_sequence_gaps(numbers) == []

    def test_single_gap(self):
        numbers = ["INV-001", "INV-002", "INV-004"]
        gaps = detect_sequence_gaps(numbers)
        assert gaps == ["INV-003"]

    def test_multiple_gaps(self):
        numbers = ["INV-001", "INV-005"]
        gaps = detect_sequence_gaps(numbers)
        assert gaps == ["INV-002", "INV-003", "INV-004"]

    def test_multiple_prefixes(self):
        numbers = ["A1", "A3", "B10", "B12"]
        gaps = detect_sequence_gaps(numbers)
        assert "A2" in gaps
        assert "B11" in gaps

    def test_non_matching_skipped(self):
        numbers = ["NO_NUMBER", "RANDOM_TEXT"]
        assert detect_sequence_gaps(numbers) == []


class TestRecordCreditSale:
    def test_record_sale_success(
        self,
        db_session: Session,
        tenant_a: Tenant,
        owner_user_a: User,
        retailer_a: Retailer,
    ):
        today = date(2026, 10, 26)
        due = date(2026, 11, 25)
        amount = 12500000  # 125,000.00 ETB

        inv, gaps = record_credit_sale(
            db_session,
            tenant_id=tenant_a.id,
            user_id=owner_user_a.id,
            invoice_number="INV-1001",
            retailer_id=retailer_a.id,
            amount_santim=amount,
            issued_date=today,
            due_date=due,
        )

        assert inv.id is not None
        assert inv.invoice_number == "INV-1001"
        assert inv.amount_santim == 12500000
        assert inv.state == InvoiceState.OPEN.value
        assert inv.issued_date == today
        assert inv.due_date == due
        assert inv.tenant_id == tenant_a.id
        assert inv.created_by == owner_user_a.id

        # Check append-only event was logged (Rule 4)
        events = db_session.execute(
            select(Event).where(Event.tenant_id == tenant_a.id, Event.aggregate_id == inv.id)
        ).scalars().all()
        assert len(events) == 1
        ev = events[0]
        assert ev.event_type == "invoice.created"
        assert ev.payload["invoice_number"] == "INV-1001"
        assert ev.payload["amount_santim"] == 12500000

    def test_reject_duplicate_invoice_number_same_tenant(
        self,
        db_session: Session,
        tenant_a: Tenant,
        owner_user_a: User,
        retailer_a: Retailer,
    ):
        today = date(2026, 10, 26)
        due = date(2026, 11, 25)

        record_credit_sale(
            db_session,
            tenant_id=tenant_a.id,
            user_id=owner_user_a.id,
            invoice_number="INV-DUPLICATE",
            retailer_id=retailer_a.id,
            amount_santim=500000,
            issued_date=today,
            due_date=due,
        )

        with pytest.raises(DuplicateInvoiceError, match="already exists"):
            record_credit_sale(
                db_session,
                tenant_id=tenant_a.id,
                user_id=owner_user_a.id,
                invoice_number="INV-DUPLICATE",
                retailer_id=retailer_a.id,
                amount_santim=700000,
                issued_date=today,
                due_date=due,
            )

    def test_permit_same_invoice_number_different_tenants(
        self,
        db_session: Session,
        tenant_a: Tenant,
        tenant_b: Tenant,
        owner_user_a: User,
        retailer_a: Retailer,
    ):
        # Create user and retailer for Tenant B
        user_b = User(
            id=uuid.uuid4(),
            tenant_id=tenant_b.id,
            role="owner",
            display_name="Beta Owner",
            created_at=datetime.now(timezone.utc),
        )
        ret_b = Retailer(
            id=uuid.uuid4(),
            tenant_id=tenant_b.id,
            name="Retailer B",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add_all([user_b, ret_b])
        db_session.flush()

        today = date(2026, 10, 26)
        due = date(2026, 11, 25)

        # Tenant A records INV-SHARED
        inv_a, _ = record_credit_sale(
            db_session,
            tenant_id=tenant_a.id,
            user_id=owner_user_a.id,
            invoice_number="INV-SHARED",
            retailer_id=retailer_a.id,
            amount_santim=500000,
            issued_date=today,
            due_date=due,
        )

        # Tenant B records same INV-SHARED successfully
        inv_b, _ = record_credit_sale(
            db_session,
            tenant_id=tenant_b.id,
            user_id=user_b.id,
            invoice_number="INV-SHARED",
            retailer_id=ret_b.id,
            amount_santim=900000,
            issued_date=today,
            due_date=due,
        )

        assert inv_a.id != inv_b.id
        assert inv_a.invoice_number == inv_b.invoice_number

    def test_reject_due_date_before_issued_date(
        self,
        db_session: Session,
        tenant_a: Tenant,
        owner_user_a: User,
        retailer_a: Retailer,
    ):
        with pytest.raises(InvalidSaleError, match="Due date .* cannot be earlier than issued date"):
            record_credit_sale(
                db_session,
                tenant_id=tenant_a.id,
                user_id=owner_user_a.id,
                invoice_number="INV-BAD-DATE",
                retailer_id=retailer_a.id,
                amount_santim=100000,
                issued_date=date(2026, 10, 26),
                due_date=date(2026, 10, 20),
            )

    def test_reject_non_integer_money(
        self,
        db_session: Session,
        tenant_a: Tenant,
        owner_user_a: User,
        retailer_a: Retailer,
    ):
        with pytest.raises(TypeError, match="integer santim"):
            record_credit_sale(
                db_session,
                tenant_id=tenant_a.id,
                user_id=owner_user_a.id,
                invoice_number="INV-FLOAT",
                retailer_id=retailer_a.id,
                amount_santim=1250.50,  # float rejected!
                issued_date=date(2026, 10, 26),
                due_date=date(2026, 11, 26),
            )

    def test_flags_sequence_gap_on_insert(
        self,
        db_session: Session,
        tenant_a: Tenant,
        owner_user_a: User,
        retailer_a: Retailer,
    ):
        today = date(2026, 10, 26)
        due = date(2026, 11, 26)

        record_credit_sale(
            db_session,
            tenant_id=tenant_a.id,
            user_id=owner_user_a.id,
            invoice_number="INV-001",
            retailer_id=retailer_a.id,
            amount_santim=100000,
            issued_date=today,
            due_date=due,
        )

        _, gaps = record_credit_sale(
            db_session,
            tenant_id=tenant_a.id,
            user_id=owner_user_a.id,
            invoice_number="INV-003",
            retailer_id=retailer_a.id,
            amount_santim=200000,
            issued_date=today,
            due_date=due,
        )

        assert "INV-002" in gaps


class TestParseAndRecordSalesCsv:
    def test_parse_sales_csv_valid_and_creates_retailer(
        self,
        db_session: Session,
        tenant_a: Tenant,
        owner_user_a: User,
    ):
        csv_data = """Invoice Number,Retailer Name,Amount,Issue Date,Due Date,Phone
INV-201,Almaz Kiosk,1500.50,2026-10-01,2026-10-31,+251911000111
INV-202,Almaz Kiosk,3200.00,2026-10-02,2026-11-01,+251911000111
INV-204,New Star Grocery,5000.00,2026-10-03,2026-11-02,+251922334455
"""
        invoices, gaps, errors = parse_and_record_sales_csv(
            db_session,
            tenant_id=tenant_a.id,
            user_id=owner_user_a.id,
            csv_content=csv_data,
        )

        assert len(invoices) == 3
        assert len(errors) == 0
        assert "INV-203" in gaps

        # Verify amounts converted correctly from ETB to santim
        assert invoices[0].amount_santim == 150050
        assert invoices[1].amount_santim == 320000
        assert invoices[2].amount_santim == 500000

        # Verify retailers created
        retailers = db_session.execute(
            select(Retailer).where(Retailer.tenant_id == tenant_a.id)
        ).scalars().all()
        names = {r.name for r in retailers}
        assert "Almaz Kiosk" in names
        assert "New Star Grocery" in names

    def test_parse_sales_csv_handles_row_errors(
        self,
        db_session: Session,
        tenant_a: Tenant,
        owner_user_a: User,
    ):
        csv_data = """Invoice Number,Retailer Name,Amount,Issue Date,Due Date
INV-301,Good Shop,1000.00,2026-10-01,2026-10-31
,Missing Invoice Num,2000.00,2026-10-01,2026-10-31
INV-302,Invalid Amount,NOT_A_NUMBER,2026-10-01,2026-10-31
INV-303,Bad Date Shop,1500.00,INVALID_DATE,2026-10-31
"""
        invoices, gaps, errors = parse_and_record_sales_csv(
            db_session,
            tenant_id=tenant_a.id,
            user_id=owner_user_a.id,
            csv_content=csv_data,
        )

        assert len(invoices) == 1
        assert invoices[0].invoice_number == "INV-301"
        assert len(errors) == 3
