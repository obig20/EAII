"""
tests/reports/test_reports.py

Tests for Aging Report, Morning Collections Brief, and CSV Exports.
Enforces:
  - Non-negotiable Rule 1: Money is integer santim.
  - Non-negotiable Rule 2: Only CONFIRMED payments reduce remaining balance.
    PROPOSED and ACCEPTED_UNVERIFIED payments NEVER reduce remaining balance
    in any total, report, or export.
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.kernel.enums import EvidenceLevel, InvoiceState, MatchState, PaymentState
from app.kernel.models import ImportBatch, Invoice, PaymentLine, PaymentMatch, Retailer, Tenant, User
from app.reports.aging import calculate_aging_bucket, generate_aging_report
from app.reports.exports import export_accountant_csv, export_payments_csv, export_sales_csv
from app.reports.morning_brief import generate_morning_brief_text


@pytest.fixture
def test_retailers(db_session: Session, tenant_a: Tenant) -> dict[str, Retailer]:
    now = datetime.now(timezone.utc)
    r1 = Retailer(id=uuid.uuid4(), tenant_id=tenant_a.id, name="Retailer Abebe", phone="+251911111111", created_at=now)
    r2 = Retailer(id=uuid.uuid4(), tenant_id=tenant_a.id, name="Retailer Bekele", phone="+251922222222", created_at=now)
    r3 = Retailer(id=uuid.uuid4(), tenant_id=tenant_a.id, name="Retailer Chala", phone="+251933333333", created_at=now)
    db_session.add_all([r1, r2, r3])
    db_session.flush()
    return {"abebe": r1, "bekele": r2, "chala": r3}


class TestAgingBuckets:
    def test_current_bucket(self):
        as_of = date(2026, 10, 26)
        bucket, days = calculate_aging_bucket(due_date=date(2026, 10, 26), as_of=as_of)
        assert bucket == "current"
        assert days == 0

        bucket, days = calculate_aging_bucket(due_date=date(2026, 11, 15), as_of=as_of)
        assert bucket == "current"
        assert days <= 0

    def test_1_30_bucket(self):
        as_of = date(2026, 10, 26)
        bucket, days = calculate_aging_bucket(due_date=date(2026, 10, 25), as_of=as_of)
        assert bucket == "1_30"
        assert days == 1

        bucket, days = calculate_aging_bucket(due_date=date(2026, 9, 26), as_of=as_of)
        assert bucket == "1_30"
        assert days == 30

    def test_31_60_bucket(self):
        as_of = date(2026, 10, 26)
        bucket, days = calculate_aging_bucket(due_date=date(2026, 9, 25), as_of=as_of)
        assert bucket == "31_60"
        assert days == 31

        bucket, days = calculate_aging_bucket(due_date=date(2026, 8, 27), as_of=as_of)
        assert bucket == "31_60"
        assert days == 60

    def test_61_90_bucket(self):
        as_of = date(2026, 10, 26)
        bucket, days = calculate_aging_bucket(due_date=date(2026, 8, 26), as_of=as_of)
        assert bucket == "61_90"
        assert days == 61

        bucket, days = calculate_aging_bucket(due_date=date(2026, 7, 28), as_of=as_of)
        assert bucket == "61_90"
        assert days == 90

    def test_90_plus_bucket(self):
        as_of = date(2026, 10, 26)
        bucket, days = calculate_aging_bucket(due_date=date(2026, 7, 27), as_of=as_of)
        assert bucket == "90_plus"
        assert days == 91


class TestAgingReport:
    def test_confirmed_payment_reduces_balance(
        self,
        db_session: Session,
        tenant_a: Tenant,
        owner_user_a: User,
        test_retailers: dict[str, Retailer],
    ):
        now = datetime.now(timezone.utc)
        ret = test_retailers["abebe"]

        inv = Invoice(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            invoice_number="INV-AG-1",
            retailer_id=ret.id,
            amount_santim=1000000,  # 10,000.00 ETB
            issued_date=date(2026, 9, 1),
            due_date=date(2026, 10, 1),  # Overdue by 25 days on 2026-10-26
            state=InvoiceState.OPEN.value,
            created_at=now,
            created_by=owner_user_a.id,
        )
        batch = ImportBatch(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            source_type="bank_statement",
            evidence_level=EvidenceLevel.L2.value,
            filename="stmt.csv",
            imported_by=owner_user_a.id,
            imported_at=now,
        )
        pl = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            import_batch_id=batch.id,
            value_date=date(2026, 10, 5),
            amount_santim=400000,  # 4,000.00 ETB
            evidence_level=EvidenceLevel.L2.value,
            state=PaymentState.CONFIRMED.value,
            created_at=now,
        )
        match = PaymentMatch(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            payment_line_id=pl.id,
            invoice_id=inv.id,
            matched_santim=400000,
            state=MatchState.CONFIRMED.value,
            tier="tier_a",
            created_at=now,
        )
        db_session.add_all([inv, batch, pl, match])
        db_session.flush()

        rep = generate_aging_report(db_session, tenant_id=tenant_a.id, as_of_date=date(2026, 10, 26))

        assert rep.grand_total_outstanding_santim == 600000  # 10,000 - 4,000 = 6,000 ETB
        assert rep.total_1_30_santim == 600000
        assert len(rep.retailers) == 1
        assert rep.retailers[0].total_outstanding_santim == 600000

    def test_proposed_and_unverified_do_not_reduce_balance(
        self,
        db_session: Session,
        tenant_a: Tenant,
        owner_user_a: User,
        test_retailers: dict[str, Retailer],
    ):
        """
        NON-NEGOTIABLE RULE 2:
        PROPOSED and ACCEPTED_UNVERIFIED payments NEVER reduce balance in aging report!
        """
        now = datetime.now(timezone.utc)
        ret = test_retailers["bekele"]

        inv = Invoice(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            invoice_number="INV-AG-RULE2",
            retailer_id=ret.id,
            amount_santim=500000,  # 5,000.00 ETB
            issued_date=date(2026, 9, 1),
            due_date=date(2026, 10, 1),
            state=InvoiceState.OPEN.value,
            created_at=now,
            created_by=owner_user_a.id,
        )
        batch = ImportBatch(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            source_type="bank_statement",
            evidence_level=EvidenceLevel.L2.value,
            filename="stmt.csv",
            imported_by=owner_user_a.id,
            imported_at=now,
        )

        # 1. Proposed match (e.g. L2 awaiting approval)
        pl_proposed = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            import_batch_id=batch.id,
            value_date=date(2026, 10, 5),
            amount_santim=200000,
            evidence_level=EvidenceLevel.L2.value,
            state=PaymentState.PROPOSED.value,
            created_at=now,
        )
        match_proposed = PaymentMatch(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            payment_line_id=pl_proposed.id,
            invoice_id=inv.id,
            matched_santim=200000,
            state=MatchState.PROPOSED.value,  # PROPOSED!
            tier="tier_b",
            created_at=now,
        )

        # 2. Accepted unverified payment (L4 screenshot)
        pl_unverified = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            import_batch_id=batch.id,
            value_date=date(2026, 10, 10),
            amount_santim=300000,
            evidence_level=EvidenceLevel.L4.value,
            state=PaymentState.ACCEPTED_UNVERIFIED.value,  # ACCEPTED_UNVERIFIED!
            created_at=now,
        )
        match_unverified = PaymentMatch(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            payment_line_id=pl_unverified.id,
            invoice_id=inv.id,
            matched_santim=300000,
            state=MatchState.PROPOSED.value,
            tier="tier_c",
            created_at=now,
        )

        db_session.add_all([inv, batch, pl_proposed, match_proposed, pl_unverified, match_unverified])
        db_session.flush()

        rep = generate_aging_report(db_session, tenant_id=tenant_a.id, as_of_date=date(2026, 10, 26))

        # Full 500,000 santim must still be outstanding! Neither proposed nor accepted_unverified counted!
        assert rep.grand_total_outstanding_santim == 500000
        assert rep.retailers[0].invoices[0].paid_santim == 0
        assert rep.retailers[0].invoices[0].remaining_santim == 500000


class TestMorningBrief:
    def test_ranking_by_overdue_days_and_amount(
        self,
        db_session: Session,
        tenant_a: Tenant,
        owner_user_a: User,
        test_retailers: dict[str, Retailer],
    ):
        as_of = date(2026, 10, 26)
        now = datetime.now(timezone.utc)

        # Abebe: 15 days overdue, 100,000 santim
        inv_abebe = Invoice(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            invoice_number="INV-MB-1",
            retailer_id=test_retailers["abebe"].id,
            amount_santim=100000,
            issued_date=date(2026, 9, 1),
            due_date=as_of - timedelta(days=15),
            state=InvoiceState.OPEN.value,
            created_at=now,
            created_by=owner_user_a.id,
        )
        # Bekele: 75 days overdue, 50,000 santim (ranked higher because 75 > 15)
        inv_bekele = Invoice(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            invoice_number="INV-MB-2",
            retailer_id=test_retailers["bekele"].id,
            amount_santim=50000,
            issued_date=date(2026, 7, 1),
            due_date=as_of - timedelta(days=75),
            state=InvoiceState.OPEN.value,
            created_at=now,
            created_by=owner_user_a.id,
        )
        # Chala: current (0 days overdue)
        inv_chala = Invoice(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            invoice_number="INV-MB-3",
            retailer_id=test_retailers["chala"].id,
            amount_santim=200000,
            issued_date=date(2026, 10, 20),
            due_date=as_of + timedelta(days=10),
            state=InvoiceState.OPEN.value,
            created_at=now,
            created_by=owner_user_a.id,
        )
        db_session.add_all([inv_abebe, inv_bekele, inv_chala])
        db_session.flush()

        brief = generate_morning_brief_text(db_session, tenant_id=tenant_a.id, as_of_date=as_of)

        assert "DAILY CLOSE — MORNING COLLECTIONS BRIEF" in brief
        assert "Total Outstanding: 3500.00 ETB" in brief
        assert "Total Overdue    : 1500.00 ETB" in brief
        assert "Current (Not Due): 2000.00 ETB" in brief

        # Bekele (75 days overdue) should be target 1
        pos_bekele = brief.find("Retailer Bekele")
        pos_abebe = brief.find("Retailer Abebe")
        assert pos_bekele != -1
        assert pos_abebe != -1
        assert pos_bekele < pos_abebe  # Bekele appears before Abebe in priority list

        # Chala is current, should not appear in priority targets
        assert "Retailer Chala" not in brief[brief.find("PRIORITY COLLECTION TARGETS:"):]

    def test_morning_brief_zero_overdue(
        self,
        db_session: Session,
        tenant_a: Tenant,
    ):
        as_of = date(2026, 10, 26)
        brief = generate_morning_brief_text(db_session, tenant_id=tenant_a.id, as_of_date=as_of)
        assert "[✓] All accounts in good standing! Zero overdue invoices." in brief


class TestExports:
    def test_export_sales_csv(
        self,
        db_session: Session,
        tenant_a: Tenant,
        owner_user_a: User,
        test_retailers: dict[str, Retailer],
    ):
        now = datetime.now(timezone.utc)
        inv = Invoice(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            invoice_number="INV-EXP-1",
            retailer_id=test_retailers["abebe"].id,
            amount_santim=850000,  # 8,500.00 ETB
            issued_date=date(2026, 10, 1),
            due_date=date(2026, 10, 31),
            state=InvoiceState.OPEN.value,
            created_at=now,
            created_by=owner_user_a.id,
        )
        db_session.add(inv)
        db_session.flush()

        csv_text = export_sales_csv(db_session, tenant_id=tenant_a.id)
        reader = list(csv.reader(io.StringIO(csv_text)))

        assert reader[0][0] == "Invoice Number"
        assert reader[0][5] == "Original Amount (Santim)"
        assert reader[0][6] == "Original Amount (ETB)"

        # Check row
        row = reader[1]
        assert row[0] == "INV-EXP-1"
        assert row[1] == "Retailer Abebe"
        assert row[5] == "850000"
        assert row[6] == "8500.00 ETB"

    def test_export_payments_csv(
        self,
        db_session: Session,
        tenant_a: Tenant,
        owner_user_a: User,
    ):
        now = datetime.now(timezone.utc)
        batch = ImportBatch(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            source_type="bank_statement",
            evidence_level=EvidenceLevel.L2.value,
            filename="stmt.csv",
            imported_by=owner_user_a.id,
            imported_at=now,
        )
        pl = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            import_batch_id=batch.id,
            value_date=date(2026, 10, 20),
            amount_santim=350000,
            evidence_level=EvidenceLevel.L2.value,
            state=PaymentState.CONFIRMED.value,
            source_ref="FT12345678",
            counterparty_name="Abebe Shop",
            raw_reference="INV-EXP-1 Payment",
            created_at=now,
        )
        db_session.add_all([batch, pl])
        db_session.flush()

        csv_text = export_payments_csv(db_session, tenant_id=tenant_a.id)
        reader = list(csv.reader(io.StringIO(csv_text)))

        assert reader[0][0] == "Payment ID"
        assert reader[0][6] == "Evidence Level"

        row = reader[1]
        assert row[2] == "FT12345678"
        assert row[3] == "Abebe Shop"
        assert row[6] == "L2"
        assert row[7] == "350000"
        assert row[8] == "3500.00 ETB"

    def test_export_accountant_csv_with_unverified_in_audit_queue(
        self,
        db_session: Session,
        tenant_a: Tenant,
        owner_user_a: User,
        test_retailers: dict[str, Retailer],
    ):
        """
        Accountant export must clearly separate confirmed sales/payments in Section 1
        from unverified (L4 accepted_unverified) in Section 2 Audit Queue.
        """
        now = datetime.now(timezone.utc)
        inv = Invoice(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            invoice_number="INV-ACC-1",
            retailer_id=test_retailers["abebe"].id,
            amount_santim=1000000,
            issued_date=date(2026, 10, 1),
            due_date=date(2026, 10, 31),
            state=InvoiceState.OPEN.value,
            created_at=now,
            created_by=owner_user_a.id,
        )
        batch = ImportBatch(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            source_type="bank_statement",
            evidence_level=EvidenceLevel.L2.value,
            filename="stmt.csv",
            imported_by=owner_user_a.id,
            imported_at=now,
        )
        # Accepted unverified L4 item
        pl = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            import_batch_id=batch.id,
            value_date=date(2026, 10, 20),
            amount_santim=500000,
            evidence_level=EvidenceLevel.L4.value,
            state=PaymentState.ACCEPTED_UNVERIFIED.value,
            source_ref="SCREENSHOT-99",
            counterparty_name="Abebe Shop",
            raw_reference="Telegram Photo Receipt",
            created_at=now,
        )
        db_session.add_all([inv, batch, pl])
        db_session.flush()

        csv_text = export_accountant_csv(db_session, tenant_id=tenant_a.id)

        assert "--- SECTION 1: SALES & RECEIVABLES RECONCILIATION ---" in csv_text
        assert "--- SECTION 2: UNMATCHED & PROPOSED ITEMS (AUDIT QUEUE) ---" in csv_text

        # Verify that in Section 1, confirmed paid is 0
        assert "TOTAL PAID (SANTIM),0" in csv_text
        assert "TOTAL OUTSTANDING (SANTIM),1000000" in csv_text

        # Verify that the accepted_unverified L4 payment is in Section 2 Audit Queue
        assert "SCREENSHOT-99" in csv_text or "accepted_unverified" in csv_text
        assert "TOTAL UNRESOLVED SANTIM,500000" in csv_text
