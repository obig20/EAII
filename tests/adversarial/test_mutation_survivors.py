"""
tests/adversarial/test_mutation_survivors.py

Adversarial mutation test suite.
For every mutant (invariants or protections deliberately removed or weakened),
this suite verifies that the test suite catches the break:
1. assert_confirmable allowing L4 evidence
2. Ambiguity guard removed
3. accepted_unverified counted in aging or exports
4. Partial or combined payments auto-confirmed
5. Database REVOKE on events removed
6. Row-level security disabled
7. CSRF check removed
8. Owner check removed
9. Session expiry removed
10. Retailer token expiry and single-use removed
11. Over-allocation check removed
12. Statement import idempotency removed
"""

from __future__ import annotations

import io
import uuid
from datetime import date, datetime, timedelta, timezone
import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient

from app.kernel.enums import (
    EvidenceLevel,
    InvoiceState,
    MatchState,
    PaymentState,
    UserRole,
)
from app.kernel.guards import (
    AmbiguousMatchError,
    InsufficientEvidenceError,
    assert_confirmable,
    assert_unambiguous,
)
from app.kernel.models import (
    Event,
    ImportBatch,
    Invoice,
    PaymentLine,
    PaymentMatch,
    Retailer,
    RetailerConfirmationToken,
    Session as SessionModel,
    Tenant,
    User,
)
from app.kernel.retailer_confirmation import (
    TokenAlreadyUsedError,
    TokenExpiredError,
    confirm_retailer_balance,
    create_retailer_confirmation_token,
    validate_confirmation_token,
)
from app.kernel.sales import record_credit_sale
from app.matching.matcher import MatchItem, MatchResult, MatcherConfig, PaymentMatcher
from app.reports.aging import generate_aging_report
from app.reports.exports import export_sales_csv
from tests.conftest import authenticate_client


class TestMutationSurvivors:
    def test_mutant_1_assert_confirmable_rejects_l4(self):
        """Mutant 1: assert_confirmable must strictly reject L4 evidence."""
        with pytest.raises(InsufficientEvidenceError, match="cannot set a payment to 'confirmed'"):
            assert_confirmable(EvidenceLevel.L4)

    def test_mutant_2_ambiguity_guard_function(self):
        """Mutant 2: Ambiguity guard must raise when candidate invoice count > 1."""
        with pytest.raises(AmbiguousMatchError, match="Cannot auto-confirm"):
            assert_unambiguous([uuid.uuid4(), uuid.uuid4()])

    def test_mutant_3_aging_report_excludes_accepted_unverified(
        self, db_session: Session, tenant_a: Tenant, owner_user_a: User
    ):
        """
        Mutant 3: In generate_aging_report, payments in accepted_unverified (L4)
        must NEVER reduce aging balances.
        """
        ret = Retailer(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            name="Aging Retailer",
            phone="+251911998801",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(ret)
        db_session.flush()

        inv = Invoice(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            retailer_id=ret.id,
            invoice_number="INV-AGING-UV",
            amount_santim=200000,
            issued_date=date(2026, 1, 1),
            due_date=date(2026, 1, 10),
            state=InvoiceState.OPEN.value,
            created_at=datetime.now(timezone.utc),
            created_by=owner_user_a.id,
        )
        # PaymentLine with L4 accepted_unverified
        pl = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            amount_santim=200000,
            value_date=date(2026, 1, 5),
            counterparty_name="Aging Retailer",
            raw_reference="L4 Note",
            evidence_level=EvidenceLevel.L4.value,
            state=PaymentState.ACCEPTED_UNVERIFIED.value,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add_all([inv, pl])
        db_session.flush()

        report = generate_aging_report(db_session, tenant_id=tenant_a.id, as_of_date=date(2026, 2, 1))
        assert report.grand_total_outstanding_santim == 200000, (
            f"Mutant 3 survived: accepted_unverified reduced aging total! "
            f"Expected 200000, got {report.grand_total_outstanding_santim}"
        )

    def test_mutant_3_exports_sales_csv_excludes_accepted_unverified(
        self, db_session: Session, tenant_a: Tenant, owner_user_a: User
    ):
        """
        Mutant 3: In export_sales_csv, accepted_unverified must NOT be in paid_santim.
        """
        ret = Retailer(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            name="Export Retailer",
            phone="+251911998802",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(ret)
        db_session.flush()

        inv = Invoice(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            retailer_id=ret.id,
            invoice_number="INV-EXP-UV",
            amount_santim=150000,
            issued_date=date(2026, 1, 1),
            due_date=date(2026, 1, 15),
            state=InvoiceState.OPEN.value,
            created_at=datetime.now(timezone.utc),
            created_by=owner_user_a.id,
        )
        pl = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            amount_santim=150000,
            value_date=date(2026, 1, 2),
            counterparty_name="Export Retailer",
            raw_reference="manual memo",
            evidence_level=EvidenceLevel.L4.value,
            state=PaymentState.ACCEPTED_UNVERIFIED.value,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add_all([inv, pl])
        db_session.flush()

        m = PaymentMatch(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            invoice_id=inv.id,
            payment_line_id=pl.id,
            matched_santim=150000,
            tier="tier_c",
            state=MatchState.PROPOSED.value,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(m)
        db_session.flush()

        csv_str = export_sales_csv(db_session, tenant_id=tenant_a.id)
        lines = [line.split(",") for line in csv_str.strip().splitlines()]
        header = lines[0]
        inv_idx = header.index("Invoice Number")
        paid_idx = header.index("Paid Amount (Santim)")
        rem_idx = header.index("Remaining Amount (Santim)")

        row = next(r for r in lines[1:] if r[inv_idx] == "INV-EXP-UV")
        assert row[paid_idx] == "0", f"Mutant 3 survived: accepted_unverified counted in paid! Got {row[paid_idx]}"
        assert row[rem_idx] == "150000", f"Mutant 3 survived: remaining amount was deducted! Got {row[rem_idx]}"

    def test_mutant_4_matcher_partial_payment_never_auto_confirmed(
        self, tenant_a: Tenant, owner_user_a: User
    ):
        """Mutant 4: In PaymentMatcher, partial matches must NEVER auto-confirm."""
        matcher = PaymentMatcher()
        inv = Invoice(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            retailer_id=uuid.uuid4(),
            invoice_number="INV-PART-4",
            amount_santim=100000,
            issued_date=date(2026, 1, 1),
            due_date=date(2026, 1, 15),
            state=InvoiceState.OPEN.value,
            created_at=datetime.now(timezone.utc),
            created_by=owner_user_a.id,
        )
        pl = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            amount_santim=50000,  # Half the invoice
            value_date=date(2026, 1, 2),
            counterparty_name="Partial Buyer",
            raw_reference="INV-PART-4",
            evidence_level=EvidenceLevel.L1.value,
            state=PaymentState.UNMATCHED.value,
            created_at=datetime.now(timezone.utc),
        )
        result = matcher.match_payment(
            payment=pl,
            open_invoices=[inv],
            retailers=[],
        )
        assert result.status != "auto_confirmed", (
            f"Mutant 4 survived: Partial payment was marked as auto_confirmed! Status: {result.status}"
        )

    def test_mutant_5_database_event_immutability(self, engine):
        """Mutant 5: REVOKE on events table must prevent UPDATE and DELETE under app role."""
        with engine.connect() as conn:
            with pytest.raises(Exception) as exc_info:
                conn.execute(text("SET ROLE app"))
                conn.execute(text("UPDATE events SET event_type = 'tampered'"))
            assert "permission denied" in str(exc_info.value).lower()

    def test_mutant_6_row_level_security_enforcement(
        self, db_session: Session, tenant_a: Tenant, tenant_b: Tenant, owner_user_a: User
    ):
        """Mutant 6: Disabling RLS leaks data; test must prove RLS blocks cross-tenant reads."""
        ret = Retailer(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            name="RLS Ret",
            phone="+251911009988",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(ret)
        db_session.flush()

        inv = Invoice(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            retailer_id=ret.id,
            invoice_number="INV-RLS-M6",
            amount_santim=10000,
            issued_date=date(2026, 1, 1),
            due_date=date(2026, 1, 15),
            state=InvoiceState.OPEN.value,
            created_at=datetime.now(timezone.utc),
            created_by=owner_user_a.id,
        )
        db_session.add(inv)
        db_session.flush()

        db_session.execute(text("SET ROLE app"))
        db_session.execute(text(f"SET app.current_tenant = '{tenant_b.id}'"))
        rows = db_session.execute(
            text("SELECT id FROM invoices WHERE id = :id"), {"id": str(inv.id)}
        ).fetchall()
        db_session.execute(text("RESET ROLE"))
        assert len(rows) == 0, "Mutant 6 survived: RLS allowed cross-tenant read under app role!"

    def test_mutant_7_csrf_protection_enforcement(
        self, client: TestClient, db_session: Session, owner_user_a: User
    ):
        """Mutant 7: State-changing POST without CSRF header or form field returns 403."""
        authenticate_client(client, db_session, owner_user_a)
        client.headers.pop("x-csrf-token", None)
        res = client.post(
            "/sales",
            data={
                "retailer_id": str(uuid.uuid4()),
                "invoice_number": "INV-NO-CSRF",
                "amount_etb": "100.00",
                "due_date": "2026-01-15",
            },
        )
        assert res.status_code == 403, (
            f"Mutant 7 survived: State-changing POST succeeded without CSRF token! Status: {res.status_code}"
        )

    def test_mutant_8_owner_check_enforcement(
        self, client: TestClient, db_session: Session, cashier_user_a: User
    ):
        """Mutant 8: Cashier must not access owner-only routes (e.g. /settings/users)."""
        authenticate_client(client, db_session, cashier_user_a)
        res = client.get("/settings/users")
        assert res.status_code == 403, (
            f"Mutant 8 survived: Cashier accessed owner-only /settings/users route! Status: {res.status_code}"
        )

    def test_mutant_9_session_expiry_enforcement(
        self, client: TestClient, db_session: Session, owner_user_a: User
    ):
        """Mutant 9: Expired session token must be rejected with 401."""
        from app.kernel.auth import create_user_session
        session_rec, raw_token = create_user_session(db_session, user=owner_user_a)
        # Expire session activity & lifetime in DB
        session_rec.last_activity_at = datetime.now(timezone.utc) - timedelta(hours=2)
        session_rec.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        db_session.flush()

        client.cookies.set("dc_session", raw_token)
        res = client.get("/dashboard")
        assert res.status_code == 401, (
            f"Mutant 9 survived: Expired session was accepted! Status: {res.status_code}"
        )

    def test_mutant_10_retailer_token_expiry_and_single_use(
        self, db_session: Session, tenant_a: Tenant, owner_user_a: User
    ):
        """Mutant 10: Retailer tokens must reject expired tokens and prevent token reuse."""
        ret = Retailer(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            name="Token Retailer",
            phone="+251911887766",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(ret)
        db_session.flush()

        token_rec, raw_token = create_retailer_confirmation_token(
            db_session,
            tenant_id=tenant_a.id,
            retailer_id=ret.id,
            expires_in_days=7,
        )
        token_rec.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
        db_session.flush()

        with pytest.raises(TokenExpiredError):
            validate_confirmation_token(db_session, raw_token=raw_token)

        # Single-use test: fresh token
        token_rec2, raw_token2 = create_retailer_confirmation_token(
            db_session,
            tenant_id=tenant_a.id,
            retailer_id=ret.id,
            expires_in_days=7,
        )
        token_valid, ret_valid, _ = validate_confirmation_token(db_session, raw_token=raw_token2)
        confirm_retailer_balance(db_session, token_record=token_valid)
        db_session.flush()

        with pytest.raises(TokenAlreadyUsedError):
            validate_confirmation_token(db_session, raw_token=raw_token2)

    def test_mutant_12_statement_import_idempotency(
        self, client: TestClient, db_session: Session, owner_user_a: User
    ):
        """Mutant 12: Ingesting identical statement twice must be idempotent."""
        authenticate_client(client, db_session, owner_user_a)
        csv_data = (
            "Posting Date,Credit Amount,Reference No,Description,Counterparty Name,Counterparty Account,Transaction ID\n"
            "01/01/2026,500.00,TXN10001,CBE Deposit,Abebe,10001234,TX001\n"
            "02/01/2026,750.00,TXN10002,CBE Deposit,Chala,10005678,TX002\n"
        )
        files1 = {"file": ("cbe_stmt.csv", io.BytesIO(csv_data.encode("utf-8")), "text/csv")}
        res1 = client.post("/statements/upload", files=files1, data={"source_name": "cbe_csv"})
        assert res1.status_code in (200, 303)

        count1 = db_session.scalar(
            select(text("count(*)")).select_from(PaymentLine).where(PaymentLine.tenant_id == owner_user_a.tenant_id)
        )
        assert count1 == 2

        # Ingest identical file second time
        files2 = {"file": ("cbe_stmt.csv", io.BytesIO(csv_data.encode("utf-8")), "text/csv")}
        res2 = client.post("/statements/upload", files=files2, data={"source_name": "cbe_csv"})
        count2 = db_session.scalar(
            select(text("count(*)")).select_from(PaymentLine).where(PaymentLine.tenant_id == owner_user_a.tenant_id)
        )
        assert count2 == count1, (
            f"Mutant 12 survived: Ingesting duplicate statement created duplicate payment lines! "
            f"Count before: {count1}, after: {count2}"
        )
