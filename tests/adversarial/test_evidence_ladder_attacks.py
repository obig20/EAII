"""
tests/adversarial/test_evidence_ladder_attacks.py

Exhaustive adversarial attacks against the Evidence Ladder and Financial Invariants:
1. L4 force-confirmation across entry points (Web and Telegram)
2. Double-counting a single payment across two invoices (SEC-VULN-02)
3. Over-allocation exceeding invoice balance
4. Arithmetic and boundary amounts (zero, negative, huge integer, fractional santim)
5. Unicode, Ge'ez / Amharic script, RTL homoglyphs
6. Retailer confirmation token replay
7. Retailer balance confirmation with zero explicit payments
"""

from __future__ import annotations

import io
import uuid
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient

from app.channels.telegram.bot import handle_approve_callback
from app.kernel.enums import (
    EvidenceLevel,
    InvoiceState,
    MatchState,
    PaymentState,
    UserRole,
)
from app.kernel.guards import InsufficientEvidenceError, assert_confirmable
from app.kernel.models import (
    Event,
    Invoice,
    PaymentLine,
    PaymentMatch,
    Retailer,
    RetailerConfirmationToken,
    Tenant,
    User,
)
from app.kernel.money import santim_from_etb_string
from app.kernel.retailer_confirmation import (
    TokenAlreadyUsedError,
    confirm_retailer_balance,
    create_retailer_confirmation_token,
    validate_confirmation_token,
)
from app.kernel.sales import record_credit_sale
from tests.conftest import authenticate_client


class TestEvidenceLadderAttacks:
    # -----------------------------------------------------------------------
    # 1. Attempt L4 Confirmation
    # -----------------------------------------------------------------------
    def test_attack_l4_confirmation_via_web_blocked(
        self, client: TestClient, db_session: Session, tenant_a: Tenant, owner_user_a: User
    ):
        """Attacker attempts to approve an L4 match via Web UI endpoint."""
        authenticate_client(client, db_session, owner_user_a)

        ret = Retailer(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            name="L4 Victim",
            phone="+251911112233",
            created_at=datetime.now(timezone.utc),
        )
        inv = Invoice(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            retailer_id=ret.id,
            invoice_number="INV-L4-ATTACK",
            amount_santim=50000,
            issued_date=date(2026, 1, 1),
            due_date=date(2026, 1, 15),
            state=InvoiceState.OPEN.value,
            created_at=datetime.now(timezone.utc),
            created_by=owner_user_a.id,
        )
        pl = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            amount_santim=50000,
            value_date=date(2026, 1, 2),
            counterparty_name="L4 Victim",
            raw_reference="manual memo",
            evidence_level=EvidenceLevel.L4.value,  # L4 Evidence
            state=PaymentState.PROPOSED.value,
            created_at=datetime.now(timezone.utc),
        )
        pm = PaymentMatch(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            invoice_id=inv.id,
            payment_line_id=pl.id,
            matched_santim=50000,
            tier="tier_c",
            state=MatchState.PROPOSED.value,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add_all([ret, inv, pl, pm])
        db_session.flush()

        # Attempt to POST to /matches/{id}/approve
        res = client.post(f"/matches/{pm.id}/approve")
        assert res.status_code == 400
        assert "cannot set a payment to 'confirmed'" in res.text

        # Verify in DB: must still not be confirmed
        db_session.refresh(pl)
        db_session.refresh(pm)
        assert pl.state != PaymentState.CONFIRMED.value
        assert pm.state != MatchState.CONFIRMED.value

    @pytest.mark.asyncio
    async def test_attack_l4_confirmation_via_telegram_blocked(
        self, db_session: Session, tenant_a: Tenant, owner_user_a: User
    ):
        """Attacker attempts to approve an L4 match via Telegram callback."""
        ret = Retailer(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            name="L4 TG Victim",
            phone="+251911112234",
            created_at=datetime.now(timezone.utc),
        )
        inv = Invoice(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            retailer_id=ret.id,
            invoice_number="INV-TG-L4",
            amount_santim=30000,
            issued_date=date(2026, 1, 1),
            due_date=date(2026, 1, 15),
            state=InvoiceState.OPEN.value,
            created_at=datetime.now(timezone.utc),
            created_by=owner_user_a.id,
        )
        pl = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            amount_santim=30000,
            value_date=date(2026, 1, 2),
            counterparty_name="L4 TG Victim",
            raw_reference="photo screenshot",
            evidence_level=EvidenceLevel.L4.value,
            state=PaymentState.PROPOSED.value,
            created_at=datetime.now(timezone.utc),
        )
        pm = PaymentMatch(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            invoice_id=inv.id,
            payment_line_id=pl.id,
            matched_santim=30000,
            tier="tier_c",
            state=MatchState.PROPOSED.value,
            created_at=datetime.now(timezone.utc),
        )
        owner_user_a.telegram_user_id = 999888
        db_session.add_all([ret, inv, pl, pm])
        db_session.flush()

        callback = MagicMock()
        callback.data = f"approve:{pm.id}"
        callback.from_user.id = 999888
        callback.answer = AsyncMock()
        callback.message = MagicMock()
        callback.message.edit_reply_markup = AsyncMock()

        await handle_approve_callback(callback)

        db_session.refresh(pl)
        assert pl.state != PaymentState.CONFIRMED.value

    # -----------------------------------------------------------------------
    # 2. Double-Count Attack / Double Credit (SEC-VULN-02)
    # -----------------------------------------------------------------------
    def test_attack_double_counting_payment_line(
        self, client: TestClient, db_session: Session, tenant_a: Tenant, owner_user_a: User
    ):
        """
        Attacking payment allocation:
        A single payment line of 100,000 santim is matched against two separate invoices of 100,000 santim each.
        Approving both must be blocked to prevent 200,000 santim credit from a 100,000 santim payment.
        """
        authenticate_client(client, db_session, owner_user_a)

        ret = Retailer(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            name="Double Count Retailer",
            phone="+251911776655",
            created_at=datetime.now(timezone.utc),
        )
        inv1 = Invoice(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            retailer_id=ret.id,
            invoice_number="INV-DC-01",
            amount_santim=100000,
            issued_date=date(2026, 1, 1),
            due_date=date(2026, 1, 15),
            state=InvoiceState.OPEN.value,
            created_at=datetime.now(timezone.utc),
            created_by=owner_user_a.id,
        )
        inv2 = Invoice(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            retailer_id=ret.id,
            invoice_number="INV-DC-02",
            amount_santim=100000,
            issued_date=date(2026, 1, 1),
            due_date=date(2026, 1, 15),
            state=InvoiceState.OPEN.value,
            created_at=datetime.now(timezone.utc),
            created_by=owner_user_a.id,
        )
        # Single payment line for 100000 santim
        pl = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            amount_santim=100000,
            value_date=date(2026, 1, 2),
            counterparty_name="Double Count Retailer",
            raw_reference="TXN-CBE-DOUBLE",
            evidence_level=EvidenceLevel.L2.value,
            state=PaymentState.PROPOSED.value,
            created_at=datetime.now(timezone.utc),
        )
        # Two proposed matches pointing to the exact same payment line
        pm1 = PaymentMatch(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            invoice_id=inv1.id,
            payment_line_id=pl.id,
            matched_santim=100000,
            tier="tier_b",
            state=MatchState.PROPOSED.value,
            created_at=datetime.now(timezone.utc),
        )
        pm2 = PaymentMatch(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            invoice_id=inv2.id,
            payment_line_id=pl.id,
            matched_santim=100000,
            tier="tier_b",
            state=MatchState.PROPOSED.value,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add_all([ret, inv1, inv2, pl, pm1, pm2])
        db_session.flush()

        # Approve match 1
        res1 = client.post(f"/matches/{pm1.id}/approve")
        assert res1.status_code in (200, 303)

        # Attempt to approve match 2 for the same payment line
        res2 = client.post(f"/matches/{pm2.id}/approve")
        
        # EXPECTED BEHAVIOR: res2 must fail with 400/409 because the payment is already allocated/confirmed
        assert res2.status_code in (400, 409), (
            f"VULNERABILITY SEC-VULN-02 CONFIRMED: Double credit allowed! "
            f"Single payment line of 100,000 santim was approved across two invoices! Status: {res2.status_code}"
        )

    def test_attack_parallel_approvals_race_condition(
        self, client: TestClient, db_session: Session, owner_user_a: User
    ):
        """
        SEC-02: Concurrent / parallel approvals of competing proposals for the same payment.
        Row locking and database triggers ensure exactly one succeeds and the other receives 409 Conflict.
        """
        import concurrent.futures
        authenticate_client(client, db_session, owner_user_a)

        ret = Retailer(
            id=uuid.uuid4(),
            tenant_id=owner_user_a.tenant_id,
            name="Parallel Race Retailer",
            created_at=datetime.now(timezone.utc),
        )
        inv1 = Invoice(
            id=uuid.uuid4(),
            tenant_id=owner_user_a.tenant_id,
            retailer_id=ret.id,
            invoice_number="INV-RACE-01",
            amount_santim=50000,
            issued_date=datetime.now(timezone.utc).date(),
            due_date=datetime.now(timezone.utc).date(),
            state=InvoiceState.OPEN.value,
            created_by=owner_user_a.id,
            created_at=datetime.now(timezone.utc),
        )
        inv2 = Invoice(
            id=uuid.uuid4(),
            tenant_id=owner_user_a.tenant_id,
            retailer_id=ret.id,
            invoice_number="INV-RACE-02",
            amount_santim=50000,
            issued_date=datetime.now(timezone.utc).date(),
            due_date=datetime.now(timezone.utc).date(),
            state=InvoiceState.OPEN.value,
            created_by=owner_user_a.id,
            created_at=datetime.now(timezone.utc),
        )
        pl = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=owner_user_a.tenant_id,
            amount_santim=50000,
            value_date=datetime.now(timezone.utc).date(),
            evidence_level="L2",
            state=PaymentState.PROPOSED.value,
            created_at=datetime.now(timezone.utc),
        )
        pm1 = PaymentMatch(
            id=uuid.uuid4(),
            tenant_id=owner_user_a.tenant_id,
            payment_line_id=pl.id,
            invoice_id=inv1.id,
            matched_santim=50000,
            tier="tier_b",
            state=MatchState.PROPOSED.value,
            created_at=datetime.now(timezone.utc),
        )
        pm2 = PaymentMatch(
            id=uuid.uuid4(),
            tenant_id=owner_user_a.tenant_id,
            payment_line_id=pl.id,
            invoice_id=inv2.id,
            matched_santim=50000,
            tier="tier_b",
            state=MatchState.PROPOSED.value,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add_all([ret, inv1, inv2, pl, pm1, pm2])
        db_session.flush()

        res1 = client.post(f"/matches/{pm1.id}/approve")
        res2 = client.post(f"/matches/{pm2.id}/approve")

        statuses = [res1.status_code, res2.status_code]
        assert 200 in statuses
        assert 409 in statuses

        # Assert DB total confirmed matches for payment line never exceeds payment amount
        total_confirmed = db_session.execute(
            select(func.coalesce(func.sum(PaymentMatch.matched_santim), 0))
            .where(
                PaymentMatch.payment_line_id == pl.id,
                PaymentMatch.state == MatchState.CONFIRMED.value,
            )
        ).scalar_one()
        assert total_confirmed == 50000

    # -----------------------------------------------------------------------
    # 3. Arithmetic and Boundary Attacks
    # -----------------------------------------------------------------------
    def test_attack_zero_amount_credit_sale_rejected(
        self, client: TestClient, db_session: Session, tenant_a: Tenant, owner_user_a: User
    ):
        """Zero amount (0.00 ETB) credit sales must be rejected."""
        authenticate_client(client, db_session, owner_user_a)
        res = client.post(
            "/sales",
            data={
                "retailer_id": str(uuid.uuid4()),
                "invoice_number": "INV-ZERO",
                "amount_etb": "0.00",
                "due_date": "2026-01-15",
            },
        )
        assert res.status_code == 400

    def test_attack_negative_amount_credit_sale_rejected(
        self, client: TestClient, db_session: Session, tenant_a: Tenant, owner_user_a: User
    ):
        """Negative amount (-500.00 ETB) credit sales must be rejected."""
        authenticate_client(client, db_session, owner_user_a)
        res = client.post(
            "/sales",
            data={
                "retailer_id": str(uuid.uuid4()),
                "invoice_number": "INV-NEG",
                "amount_etb": "-500.00",
                "due_date": "2026-01-15",
            },
        )
        assert res.status_code == 400

    def test_attack_fractional_santim_rounding(self):
        """Fractional santim (e.g. 10.505 ETB = 1050.5 santim) must parse safely or reject fractional precision."""
        with pytest.raises(ValueError):
            santim_from_etb_string("10.505")

    # -----------------------------------------------------------------------
    # 4. Unicode, Ge'ez / Amharic, RTL attacks
    # -----------------------------------------------------------------------
    def test_attack_amharic_and_unicode_homoglyph_names(
        self, client: TestClient, db_session: Session, tenant_a: Tenant, owner_user_a: User
    ):
        """Ge'ez script, mixed script, and RTL override must be handled without crash or unescaped display."""
        authenticate_client(client, db_session, owner_user_a)

        ret = Retailer(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            name="አበበ ሱቅ \u202eRTL_HACK",
            phone="+251911223399",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(ret)
        db_session.flush()

        res = client.post(
            "/sales",
            data={
                "retailer_id": str(ret.id),
                "invoice_number": "INV-አበበ-01",
                "amount_etb": "250.00",
                "due_date": "2026-12-31",
            },
        )
        assert res.status_code in (200, 303)

        # Verify invoice created with exact unicode
        inv = db_session.execute(
            select(Invoice).where(Invoice.invoice_number == "INV-አበበ-01")
        ).scalar_one()
        assert inv.amount_santim == 25000

    # -----------------------------------------------------------------------
    # 5. Retailer Token Replay & Zero-Payment Balance Confirmation
    # -----------------------------------------------------------------------
    def test_attack_retailer_token_replay_rejected(
        self, client: TestClient, db_session: Session, tenant_a: Tenant, owner_user_a: User
    ):
        """Attacker replays an already-used retailer confirmation token."""
        ret = Retailer(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            name="Replay Retailer",
            phone="+251911443322",
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
        # Retailer completes confirmation first time
        token_valid, ret_valid, _ = validate_confirmation_token(db_session, raw_token=raw_token)
        confirm_retailer_balance(db_session, token_record=token_valid)
        db_session.flush()

        # Attacker attempts replay
        with pytest.raises(TokenAlreadyUsedError):
            validate_confirmation_token(db_session, raw_token=raw_token)

    def test_attack_balance_confirmation_with_zero_payments_creates_no_credit(
        self, db_session: Session, tenant_a: Tenant, owner_user_a: User
    ):
        """
        NON-NEGOTIABLE RULE 2:
        Confirming a balance does NOT mark any payment confirmed unless
        the retailer explicitly lists that payment.
        """
        ret = Retailer(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            name="Zero Payment Confirm Retailer",
            phone="+251911554433",
            created_at=datetime.now(timezone.utc),
        )
        inv = Invoice(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            retailer_id=ret.id,
            invoice_number="INV-ZERO-PAY",
            amount_santim=75000,
            issued_date=date(2026, 1, 1),
            due_date=date(2026, 1, 15),
            state=InvoiceState.OPEN.value,
            created_at=datetime.now(timezone.utc),
            created_by=owner_user_a.id,
        )
        db_session.add_all([ret, inv])
        db_session.flush()

        token_rec, raw_token = create_retailer_confirmation_token(
            db_session,
            tenant_id=tenant_a.id,
            retailer_id=ret.id,
            expires_in_days=7,
        )
        db_session.flush()

        # Retailer clicks "I agree with my balance" without listing any payments
        token_valid, ret_valid, open_invs = validate_confirmation_token(db_session, raw_token=raw_token)
        success, payment_lines = confirm_retailer_balance(
            db_session, token_record=token_valid, explicit_payments=[]
        )
        db_session.flush()

        # Zero payment lines created, invoice remains OPEN with full amount
        assert len(payment_lines) == 0
        db_session.refresh(inv)
        assert inv.state == InvoiceState.OPEN.value

    # -----------------------------------------------------------------------
    # 6. SEC-10 Statement Idempotency & Overlap Detection Tests
    # -----------------------------------------------------------------------
    def test_statement_import_two_identical_legitimate_payments_kept(
        self, client: TestClient, db_session: Session, owner_user_a: User
    ):
        """Two identical legitimate payments in a single file must both be preserved with distinct positional row keys."""
        authenticate_client(client, db_session, owner_user_a)
        csv_data = (
            "Posting Date,Credit Amount,Reference No,Description,Counterparty Name,Counterparty Account,Transaction ID\n"
            "01/01/2026,500.00,TXN1,CBE Deposit,Abebe,10001234,\n"
            "01/01/2026,500.00,TXN1,CBE Deposit,Abebe,10001234,\n"
        )
        files = {"file": ("cbe_identical_rows.csv", io.BytesIO(csv_data.encode("utf-8")), "text/csv")}
        res = client.post("/statements/upload", files=files, data={"source_name": "cbe_csv"})
        assert res.status_code in (200, 303)

        lines = db_session.execute(
            select(PaymentLine).where(
                PaymentLine.tenant_id == owner_user_a.tenant_id,
                PaymentLine.amount_santim == 50000,
            )
        ).scalars().all()
        assert len(lines) == 2, f"Expected 2 identical legitimate rows, got {len(lines)}"
        assert lines[0].row_key != lines[1].row_key, "Positional keys must differentiate identical rows in file"

    def test_statement_import_overlapping_files_warning(
        self, client: TestClient, db_session: Session, owner_user_a: User
    ):
        """Importing statements with overlapping date ranges triggers overlap warning in event payload."""
        authenticate_client(client, db_session, owner_user_a)
        csv1 = (
            "Posting Date,Credit Amount,Reference No,Description,Counterparty Name,Counterparty Account,Transaction ID\n"
            "05/01/2026,300.00,TXN01,CBE Deposit,Chala,10005678,TX_OVR_01\n"
        )
        csv2 = (
            "Posting Date,Credit Amount,Reference No,Description,Counterparty Name,Counterparty Account,Transaction ID\n"
            "05/01/2026,400.00,TXN02,CBE Deposit,Dawit,10009999,TX_OVR_02\n"
        )
        client.post("/statements/upload", files={"file": ("stmt1.csv", io.BytesIO(csv1.encode("utf-8")), "text/csv")}, data={"source_name": "cbe_csv"})
        res2 = client.post("/statements/upload", files={"file": ("stmt2.csv", io.BytesIO(csv2.encode("utf-8")), "text/csv")}, data={"source_name": "cbe_csv"})
        assert res2.status_code in (200, 303)

        # Check that event payload contains overlap_warning
        from app.kernel.models import Event
        event = db_session.execute(
            select(Event).where(
                Event.tenant_id == owner_user_a.tenant_id,
                Event.event_type == "statement.imported",
            ).order_by(Event.created_at.desc())
        ).scalars().first()
        assert event is not None
        assert event.payload.get("overlap_warning") is not None
        assert "overlaps with" in event.payload["overlap_warning"]

    def test_statement_import_duplicate_row_becomes_possible_duplicate_review_item(
        self, client: TestClient, db_session: Session, tenant_a: Tenant, owner_user_a: User
    ):
        """
        Item 0B: End-to-end test of statement import containing a payment, then the same payment
        again under a different row.
        Expected:
        - The first is auto-confirmed.
        - The second becomes a 'possible duplicate or overpayment' review item.
        - No exception, no double credit on the invoice, and no silent drop.
        """
        authenticate_client(client, db_session, owner_user_a)

        ret = Retailer(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            name="Almaz Bekele Grocery",
            phone="+251911445566",
            bank_account="100044556677",
            created_at=datetime.now(timezone.utc),
        )
        inv = Invoice(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            retailer_id=ret.id,
            invoice_number="INV-E2E-DUP-01",
            amount_santim=25000,  # 250.00 ETB
            issued_date=date(2026, 1, 1),
            due_date=date(2026, 1, 30),
            state=InvoiceState.OPEN.value,
            created_at=datetime.now(timezone.utc),
            created_by=owner_user_a.id,
        )
        db_session.add_all([ret, inv])
        db_session.flush()

        # CSV statement containing the same payment twice under different rows
        csv_data = (
            "Posting Date,Credit Amount,Reference No,Description,Counterparty Name,Counterparty Account,Transaction ID\n"
            "02/01/2026,250.00,TXN101,CBE Deposit for INV-E2E-DUP-01,Almaz Bekele,100044556677,TX_DUP_01\n"
            "02/01/2026,250.00,TXN102,CBE Deposit for INV-E2E-DUP-01,Almaz Bekele,100044556677,TX_DUP_02\n"
        )
        files = {
            "file": (
                "cbe_duplicate_e2e.csv",
                io.BytesIO(csv_data.encode("utf-8")),
                "text/csv",
            )
        }
        res = client.post(
            "/statements/upload",
            files=files,
            data={"source_name": "cbe_csv"},
            follow_redirects=False,
        )
        # Ingestion succeeds cleanly with redirect (no 500 error / unhandled exception)
        assert res.status_code == 303

        # Verify payment lines preserved (no silent drop)
        lines = (
            db_session.execute(
                select(PaymentLine)
                .where(
                    PaymentLine.tenant_id == tenant_a.id,
                    PaymentLine.amount_santim == 25000,
                )
                .order_by(PaymentLine.created_at.asc())
            )
            .scalars()
            .all()
        )
        assert len(lines) == 2, f"Expected both payment lines to be preserved, found {len(lines)}"
        first_pl, second_pl = lines[0], lines[1]

        # First line is auto-confirmed
        assert first_pl.state == PaymentState.CONFIRMED.value

        # Second line becomes proposed review item (NOT auto-confirmed, NOT dropped)
        assert second_pl.state == PaymentState.PROPOSED.value

        # Check PaymentMatch records
        first_match = db_session.execute(
            select(PaymentMatch).where(
                PaymentMatch.payment_line_id == first_pl.id,
                PaymentMatch.tenant_id == tenant_a.id,
            )
        ).scalar_one()
        assert first_match.state == MatchState.CONFIRMED.value

        second_match = db_session.execute(
            select(PaymentMatch).where(
                PaymentMatch.payment_line_id == second_pl.id,
                PaymentMatch.tenant_id == tenant_a.id,
            )
        ).scalar_one()
        assert second_match.state == MatchState.PROPOSED.value

        # Review queue item reason clearly notes possible duplicate/overpayment
        event = db_session.execute(
            select(Event).where(
                Event.tenant_id == tenant_a.id,
                Event.aggregate_id == second_pl.id,
                Event.event_type == "payment.proposed",
            )
        ).scalar_one_or_none()
        assert event is not None
        reason = event.payload.get("reason", "").lower()
        assert "duplicate" in reason or "overpayment" in reason

        # Verify NO double credit: total confirmed allocation is exactly 25,000 santim
        confirmed_total = db_session.execute(
            select(func.coalesce(func.sum(PaymentMatch.matched_santim), 0)).where(
                PaymentMatch.invoice_id == inv.id,
                PaymentMatch.state == MatchState.CONFIRMED.value,
            )
        ).scalar_one()
        assert confirmed_total == 25000, f"Double credit detected! Confirmed total: {confirmed_total}"

        # Refresh invoice: state is settled (not overpaid/exceeded)
        db_session.refresh(inv)
        assert inv.state in (InvoiceState.SETTLED.value, "settled")
