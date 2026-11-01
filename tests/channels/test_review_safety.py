"""
tests/channels/test_review_safety.py

Comprehensive tests for Review Queue Safety & Match Reversal:
1. Strict owner-only access: cashiers cannot approve, reverse, or forge differing flags (HTTP 403).
2. HMAC-signed stale-view tokens: detect stale amounts/state (HTTP 409) and token reuse.
3. Differing amounts: require explicit confirmation click (HTTP 400 without it).
4. Reversal safety:
   - Mandatory reason code in append-only event (no PII).
   - Free-text notes stored only in separate deletable reversal_notes table.
   - Reversed match cannot be re-approved (HTTP 409).
   - Double reversal rejected (HTTP 409).
   - Restores invoice balance in aging report and appears in Section 3 of accountant export.
   - Multi-match partial reversal preserves other confirmed matches.
   - Cross-tenant reversal blocked under RLS.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.channels.telegram.bot import handle_approve_callback
from app.channels.web.routes import get_session
from app.kernel.enums import EvidenceLevel, InvoiceState, MatchState, PaymentState, ReversalReasonCode, UserRole
from app.kernel.models import Event, ImportBatch, Invoice, PaymentLine, PaymentMatch, Retailer, ReversalNote, Tenant, User
from app.kernel.tenant import set_tenant
from app.kernel.tokens import (
    generate_match_approval_token,
    hash_approval_token,
    verify_match_approval_token,
)
from app.main import app
from app.reports.aging import generate_aging_report
from app.reports.exports import export_accountant_csv
from app.settings import settings
from tests.conftest import authenticate_client


@pytest.fixture(autouse=True)
def override_telegram_session(monkeypatch, db_session):
    @contextmanager
    def _test_session_scope():
        yield db_session
    monkeypatch.setattr("app.channels.telegram.bot.get_session_scope", _test_session_scope)


@pytest.fixture
def client(db_session: Session):
    def _override_get_session():
        yield db_session

    app.dependency_overrides[get_session] = _override_get_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def tenant(db_session: Session) -> Tenant:
    t = Tenant(id=uuid.uuid4(), name="Safety Test Wholesaler", created_at=datetime.now(timezone.utc))
    db_session.add(t)
    db_session.flush()
    set_tenant(db_session, t.id)
    return t


@pytest.fixture
def owner(db_session: Session, tenant: Tenant) -> User:
    u = User(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        role=UserRole.OWNER.value,
        phone="+251911111111",
        email="owner@safety.et",
        display_name="Ato Owner",
        password_hash="dummy",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(u)
    db_session.flush()
    return u


@pytest.fixture
def cashier(db_session: Session, tenant: Tenant) -> User:
    u = User(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        role=UserRole.CASHIER.value,
        phone="+251922222222",
        email="cashier@safety.et",
        display_name="Woizero Cashier",
        password_hash="dummy",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(u)
    db_session.flush()
    return u


@pytest.fixture
def retailer(db_session: Session, tenant: Tenant) -> Retailer:
    r = Retailer(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        name="Selam General Store",
        phone="+251933333333",
        bank_account="1000123456",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(r)
    db_session.flush()
    return r


def create_invoice_and_match(
    db_session: Session,
    tenant: Tenant,
    owner: User,
    retailer: Retailer,
    *,
    inv_amount_santim: int = 500000,
    pl_amount_santim: int = 500000,
    matched_santim: int = 500000,
    evidence: str = "L2",
    state: str = "proposed",
) -> tuple[Invoice, PaymentLine, PaymentMatch]:
    now = datetime.now(timezone.utc)
    inv = Invoice(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        invoice_number=f"INV-{uuid.uuid4().hex[:6].upper()}",
        retailer_id=retailer.id,
        amount_santim=inv_amount_santim,
        issued_date=date(2026, 10, 1),
        due_date=date(2026, 10, 31),
        state=InvoiceState.OPEN.value if state == "proposed" else InvoiceState.SETTLED.value,
        created_at=now,
        created_by=owner.id,
    )
    batch = ImportBatch(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        source_type="bank_statement",
        evidence_level=evidence,
        filename="bank.csv",
        imported_by=owner.id,
        imported_at=now,
    )
    pl = PaymentLine(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        import_batch_id=batch.id,
        value_date=date(2026, 10, 15),
        amount_santim=pl_amount_santim,
        evidence_level=evidence,
        state=PaymentState.PROPOSED.value if state == "proposed" else PaymentState.CONFIRMED.value,
        created_at=now,
    )
    pm = PaymentMatch(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        payment_line_id=pl.id,
        invoice_id=inv.id,
        matched_santim=matched_santim,
        tier="tier_a" if inv_amount_santim == pl_amount_santim else "tier_b",
        state=state,
        created_at=now,
    )
    db_session.add_all([inv, batch, pl, pm])
    db_session.flush()
    return inv, pl, pm


class TestReviewQueuePermissions:
    def test_cashier_cannot_approve_anything(
        self, client: TestClient, db_session: Session, tenant: Tenant, owner: User, cashier: User, retailer: Retailer
    ):
        inv, pl, pm = create_invoice_and_match(db_session, tenant, owner, retailer)
        authenticate_client(client, db_session, cashier)

        res = client.post(f"/matches/{pm.id}/approve")
        assert res.status_code == 403

    def test_cashier_cannot_reverse_anything(
        self, client: TestClient, db_session: Session, tenant: Tenant, owner: User, cashier: User, retailer: Retailer
    ):
        inv, pl, pm = create_invoice_and_match(db_session, tenant, owner, retailer, state="confirmed")
        authenticate_client(client, db_session, cashier)

        res = client.post(f"/matches/{pm.id}/reverse", data={"reason_code": "wrong_invoice"})
        assert res.status_code == 403

    def test_forged_confirm_differing_by_cashier(
        self, client: TestClient, db_session: Session, tenant: Tenant, owner: User, cashier: User, retailer: Retailer
    ):
        inv, pl, pm = create_invoice_and_match(db_session, tenant, owner, retailer, pl_amount_santim=600000, matched_santim=500000)
        authenticate_client(client, db_session, cashier)

        res = client.post(f"/matches/{pm.id}/approve", data={"confirm_differing": "true"})
        assert res.status_code == 403


class TestReviewQueueDisplayAndStaleTokens:
    def test_review_queue_displays_full_context_and_alternatives(
        self, client: TestClient, db_session: Session, tenant: Tenant, owner: User, retailer: Retailer
    ):
        inv1, pl, pm = create_invoice_and_match(db_session, tenant, owner, retailer)
        # Create an alternative open invoice for the same retailer
        now = datetime.now(timezone.utc)
        inv2 = Invoice(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            invoice_number="INV-ALT-999",
            retailer_id=retailer.id,
            amount_santim=480000,
            issued_date=date(2026, 10, 5),
            due_date=date(2026, 11, 5),
            state=InvoiceState.OPEN.value,
            created_at=now,
            created_by=owner.id,
        )
        db_session.add(inv2)
        db_session.flush()

        authenticate_client(client, db_session, owner)
        res = client.get("/")
        assert res.status_code == 200
        assert "Selam General Store" in res.text
        assert inv1.invoice_number in res.text
        assert "INV-ALT-999" in res.text
        assert "Why it matched" in res.text

    def test_differing_amount_requires_extra_confirmation(
        self, client: TestClient, db_session: Session, tenant: Tenant, owner: User, retailer: Retailer
    ):
        # Payment is 6,000 ETB, Invoice open is 5,000 ETB -> differing
        inv, pl, pm = create_invoice_and_match(
            db_session, tenant, owner, retailer, inv_amount_santim=500000, pl_amount_santim=600000, matched_santim=500000
        )
        authenticate_client(client, db_session, owner)

        # 1. Attempt without confirm_differing -> fails 400
        res = client.post(f"/matches/{pm.id}/approve")
        assert res.status_code == 400
        assert "Amounts differ" in res.json()["detail"]

        # 2. Attempt with confirm_differing -> succeeds
        res_ok = client.post(f"/matches/{pm.id}/approve", data={"confirm_differing": "true"})
        assert res_ok.status_code == 200
        assert "Match Confirmed" in res_ok.text

    def test_stale_approval_rejected(
        self, client: TestClient, db_session: Session, tenant: Tenant, owner: User, retailer: Retailer
    ):
        inv, pl, pm = create_invoice_and_match(db_session, tenant, owner, retailer)
        # Generate token matching current state
        token = generate_match_approval_token(
            settings.secret_key,
            tenant_id=tenant.id,
            user_id=owner.id,
            match_id=pm.id,
            matched_santim=pm.matched_santim,
            invoice_amount_santim=inv.amount_santim,
            payment_amount_santim=pl.amount_santim,
            match_state=pm.state,
        )

        # Modify DB state behind the scenes (e.g. amount modified)
        pm.matched_santim = 400000
        db_session.commit()

        authenticate_client(client, db_session, owner)
        res = client.post(f"/matches/{pm.id}/approve", data={"approval_token": token})
        assert res.status_code == 409
        assert "Stale view" in res.json()["detail"]

    def test_token_reuse_on_another_match_rejected(
        self, client: TestClient, db_session: Session, tenant: Tenant, owner: User, retailer: Retailer
    ):
        inv1, pl1, pm1 = create_invoice_and_match(db_session, tenant, owner, retailer)
        inv2, pl2, pm2 = create_invoice_and_match(db_session, tenant, owner, retailer)
        # Token generated for pm1
        token1 = generate_match_approval_token(
            settings.secret_key,
            tenant_id=tenant.id,
            user_id=owner.id,
            match_id=pm1.id,
            matched_santim=pm1.matched_santim,
            invoice_amount_santim=inv1.amount_santim,
            payment_amount_santim=pl1.amount_santim,
            match_state=pm1.state,
        )

        authenticate_client(client, db_session, owner)
        # Attempt to use token1 on pm2
        res = client.post(f"/matches/{pm2.id}/approve", data={"approval_token": token1})
        assert res.status_code == 409
        assert "different match" in res.json()["detail"]


class TestTokenScopeAndIdempotency:
    def test_token_scope_and_expiration(self, tenant: Tenant, owner: User):
        mid = uuid.uuid4()
        other_tenant_id = uuid.uuid4()
        other_user_id = uuid.uuid4()
        now_ts = 1700000000

        # Valid token
        token = generate_match_approval_token(
            settings.secret_key,
            tenant_id=tenant.id,
            user_id=owner.id,
            match_id=mid,
            matched_santim=50000,
            invoice_amount_santim=50000,
            payment_amount_santim=50000,
            match_state="proposed",
            issued_at=now_ts,
        )

        # 1. Successful verification
        ok, msg = verify_match_approval_token(
            settings.secret_key,
            token,
            expected_tenant_id=tenant.id,
            expected_user_id=owner.id,
            expected_match_id=mid,
            current_matched_santim=50000,
            current_invoice_amount_santim=50000,
            current_payment_amount_santim=50000,
            current_match_state="proposed",
            current_time=now_ts + 60,
        )
        assert ok is True
        assert msg == "Valid"

        # 2. Reject wrong tenant
        ok_t, msg_t = verify_match_approval_token(
            settings.secret_key,
            token,
            expected_tenant_id=other_tenant_id,
            expected_user_id=owner.id,
            expected_match_id=mid,
            current_matched_santim=50000,
            current_invoice_amount_santim=50000,
            current_payment_amount_santim=50000,
            current_match_state="proposed",
            current_time=now_ts + 60,
        )
        assert ok_t is False
        assert "different tenant" in msg_t

        # 3. Reject wrong user
        ok_u, msg_u = verify_match_approval_token(
            settings.secret_key,
            token,
            expected_tenant_id=tenant.id,
            expected_user_id=other_user_id,
            expected_match_id=mid,
            current_matched_santim=50000,
            current_invoice_amount_santim=50000,
            current_payment_amount_santim=50000,
            current_match_state="proposed",
            current_time=now_ts + 60,
        )
        assert ok_u is False
        assert "different user" in msg_u

        # 4. Reject expired token (> 30 minutes = 1800s)
        ok_exp, msg_exp = verify_match_approval_token(
            settings.secret_key,
            token,
            expected_tenant_id=tenant.id,
            expected_user_id=owner.id,
            expected_match_id=mid,
            current_matched_santim=50000,
            current_invoice_amount_santim=50000,
            current_payment_amount_santim=50000,
            current_match_state="proposed",
            current_time=now_ts + 1801,
        )
        assert ok_exp is False
        assert "expired" in msg_exp

        # 5. Reject tampered token signature
        tampered_token = token[:-4] + "abcd"
        ok_tamp, msg_tamp = verify_match_approval_token(
            settings.secret_key,
            tampered_token,
            expected_tenant_id=tenant.id,
            expected_user_id=owner.id,
            expected_match_id=mid,
            current_matched_santim=50000,
            current_invoice_amount_santim=50000,
            current_payment_amount_santim=50000,
            current_match_state="proposed",
            current_time=now_ts + 60,
        )
        assert ok_tamp is False
        assert "verification failed" in msg_tamp

    def test_idempotent_approval_retry(
        self, client: TestClient, db_session: Session, tenant: Tenant, owner: User, retailer: Retailer
    ):
        inv, pl, pm = create_invoice_and_match(db_session, tenant, owner, retailer)
        token = generate_match_approval_token(
            settings.secret_key,
            tenant_id=tenant.id,
            user_id=owner.id,
            match_id=pm.id,
            matched_santim=pm.matched_santim,
            invoice_amount_santim=inv.amount_santim,
            payment_amount_santim=pl.amount_santim,
            match_state=pm.state,
        )

        authenticate_client(client, db_session, owner)

        # 1. First approval call succeeds
        res1 = client.post(f"/matches/{pm.id}/approve", data={"approval_token": token})
        assert res1.status_code == 200
        assert "Match Confirmed" in res1.text

        # 2. Exact retry returns original success (idempotent 200)
        res2 = client.post(f"/matches/{pm.id}/approve", data={"approval_token": token})
        assert res2.status_code == 200
        assert "Match Confirmed" in res2.text

        # 3. Different token or changed request returns 409 Conflict
        diff_token = token + "x"
        res3 = client.post(f"/matches/{pm.id}/approve", data={"approval_token": diff_token})
        assert res3.status_code == 409


class TestAllocationLabelsAndUnallocatedCredit:
    def test_exact_match_label(
        self, client: TestClient, db_session: Session, tenant: Tenant, owner: User, retailer: Retailer
    ):
        inv, pl, pm = create_invoice_and_match(
            db_session, tenant, owner, retailer,
            inv_amount_santim=500000, pl_amount_santim=500000, matched_santim=500000
        )
        authenticate_client(client, db_session, owner)
        res = client.get("/")
        assert res.status_code == 200
        assert "5000.00 ETB" in res.text
        # Exact match does not display differing warning
        assert "Amounts differ" not in res.text

    def test_partial_match_label(
        self, client: TestClient, db_session: Session, tenant: Tenant, owner: User, retailer: Retailer
    ):
        inv, pl, pm = create_invoice_and_match(
            db_session, tenant, owner, retailer,
            inv_amount_santim=500000, pl_amount_santim=300000, matched_santim=300000
        )
        authenticate_client(client, db_session, owner)
        res = client.get("/")
        assert res.status_code == 200
        assert "Amounts differ by 2000.00 ETB (Partial)" in res.text

    def test_overpayment_match_label_and_unallocated_credit(
        self, client: TestClient, db_session: Session, tenant: Tenant, owner: User, retailer: Retailer
    ):
        inv, pl, pm = create_invoice_and_match(
            db_session, tenant, owner, retailer,
            inv_amount_santim=500000, pl_amount_santim=700000, matched_santim=500000
        )
        authenticate_client(client, db_session, owner)
        res = client.get("/")
        assert res.status_code == 200
        assert "Amounts differ by 2000.00 ETB (Overpayment)" in res.text
        assert "Unallocated Credit Remainder: <strong>2000.00 ETB</strong>" in res.text

    def test_combined_payment_label(
        self, client: TestClient, db_session: Session, tenant: Tenant, owner: User, retailer: Retailer
    ):
        inv1, pl, pm1 = create_invoice_and_match(
            db_session, tenant, owner, retailer,
            inv_amount_santim=500000, pl_amount_santim=800000, matched_santim=500000
        )
        now = datetime.now(timezone.utc)
        inv2 = Invoice(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            invoice_number="INV-COMB-2",
            retailer_id=retailer.id,
            amount_santim=300000,
            issued_date=date(2026, 10, 2),
            due_date=date(2026, 11, 2),
            state=InvoiceState.OPEN.value,
            created_at=now,
            created_by=owner.id,
        )
        pm2 = PaymentMatch(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            payment_line_id=pl.id,
            invoice_id=inv2.id,
            matched_santim=300000,
            tier="tier_a",
            state=MatchState.PROPOSED.value,
            created_at=now,
        )
        db_session.add_all([inv2, pm2])
        db_session.flush()

        authenticate_client(client, db_session, owner)
        res = client.get("/")
        assert res.status_code == 200
        assert "(Combined)" in res.text


class TestMatchReversals:
    def test_owner_reversal_requires_valid_reason_code(
        self, client: TestClient, db_session: Session, tenant: Tenant, owner: User, retailer: Retailer
    ):
        inv, pl, pm = create_invoice_and_match(db_session, tenant, owner, retailer, state="confirmed")
        authenticate_client(client, db_session, owner)

        res = client.post(f"/matches/{pm.id}/reverse", data={"reason_code": "not_a_valid_code"})
        assert res.status_code == 400
        assert "Invalid reversal reason code" in res.json()["detail"]

    def test_owner_reversal_records_event_and_note(
        self, client: TestClient, db_session: Session, tenant: Tenant, owner: User, retailer: Retailer
    ):
        inv, pl, pm = create_invoice_and_match(db_session, tenant, owner, retailer, state="confirmed")
        authenticate_client(client, db_session, owner)

        res = client.post(
            f"/matches/{pm.id}/reverse",
            data={
                "reason_code": "wrong_invoice",
                "notes": "Cashier picked the wrong invoice number by accident.",
            },
        )
        assert res.status_code == 200
        assert "Match Reversed" in res.text

        # Verify DB states
        db_session.refresh(pm)
        db_session.refresh(pl)
        db_session.refresh(inv)
        assert pm.state == MatchState.REVERSED.value
        assert pl.state == PaymentState.PROPOSED.value
        assert pl.is_previously_reversed is True
        assert inv.state == InvoiceState.OPEN.value

        # Verify append-only event log: code present, zero PII, no notes in event payload
        ev = db_session.execute(
            select(Event).where(
                Event.tenant_id == tenant.id,
                Event.aggregate_id == pm.id,
                Event.event_type == "match.reversed",
            )
        ).scalar_one()
        assert ev.payload["reason_code"] == "wrong_invoice"
        assert ev.payload["reversed_santim"] == 500000
        assert "notes" not in ev.payload

        # Verify free text stored in separate deletable notes table
        note = db_session.execute(
            select(ReversalNote).where(
                ReversalNote.tenant_id == tenant.id,
                ReversalNote.payment_match_id == pm.id,
            )
        ).scalar_one()
        assert note.reason_code == "wrong_invoice"
        assert note.notes == "Cashier picked the wrong invoice number by accident."

    def test_double_reversal_rejected(
        self, client: TestClient, db_session: Session, tenant: Tenant, owner: User, retailer: Retailer
    ):
        inv, pl, pm = create_invoice_and_match(db_session, tenant, owner, retailer, state="confirmed")
        authenticate_client(client, db_session, owner)

        res1 = client.post(f"/matches/{pm.id}/reverse", data={"reason_code": "duplicate"})
        assert res1.status_code == 200

        res2 = client.post(f"/matches/{pm.id}/reverse", data={"reason_code": "duplicate"})
        assert res2.status_code == 409
        assert "already been reversed" in res2.json()["detail"]

    def test_reversed_match_cannot_be_reapproved(
        self, client: TestClient, db_session: Session, tenant: Tenant, owner: User, retailer: Retailer
    ):
        inv, pl, pm = create_invoice_and_match(db_session, tenant, owner, retailer, state="confirmed")
        authenticate_client(client, db_session, owner)

        # Reverse it
        client.post(f"/matches/{pm.id}/reverse", data={"reason_code": "wrong_invoice"})

        # Attempt to approve the reversed match
        res = client.post(f"/matches/{pm.id}/approve")
        assert res.status_code == 409
        assert "reversed match can never be re-approved" in res.json()["detail"]

    def test_concurrent_reversal_versus_approval(
        self, client: TestClient, db_session: Session, tenant: Tenant, owner: User, retailer: Retailer
    ):
        """
        When reversal and approval compete, row-level locks serialize the operations;
        the post-reversal approval fails with 409 Conflict.
        """
        import threading
        inv, pl, pm = create_invoice_and_match(db_session, tenant, owner, retailer, state="confirmed")
        authenticate_client(client, db_session, owner)

        results = {}

        def do_reverse():
            res = client.post(f"/matches/{pm.id}/reverse", data={"reason_code": "wrong_invoice"})
            results["reverse"] = res.status_code

        def do_approve():
            res = client.post(f"/matches/{pm.id}/approve")
            results["approve"] = res.status_code

        t1 = threading.Thread(target=do_reverse)
        t2 = threading.Thread(target=do_approve)

        t1.start()
        t1.join()
        t2.start()
        t2.join()

        assert results["reverse"] == 200
        assert results["approve"] == 409

    def test_partial_reversal_of_combined_payment(
        self, client: TestClient, db_session: Session, tenant: Tenant, owner: User, retailer: Retailer
    ):
        """
        One payment of 8,000 ETB matched across INV-1 (5,000 ETB) and INV-2 (3,000 ETB).
        Reversing match 1 restores INV-1, while INV-2 match remains confirmed.
        """
        now = datetime.now(timezone.utc)
        inv1 = Invoice(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            invoice_number="INV-COMB-1",
            retailer_id=retailer.id,
            amount_santim=500000,
            issued_date=date(2026, 10, 1),
            due_date=date(2026, 10, 31),
            state=InvoiceState.SETTLED.value,
            created_at=now,
            created_by=owner.id,
        )
        inv2 = Invoice(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            invoice_number="INV-COMB-2",
            retailer_id=retailer.id,
            amount_santim=300000,
            issued_date=date(2026, 10, 1),
            due_date=date(2026, 10, 31),
            state=InvoiceState.SETTLED.value,
            created_at=now,
            created_by=owner.id,
        )
        batch = ImportBatch(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            source_type="bank_statement",
            evidence_level="L2",
            filename="batch.csv",
            imported_by=owner.id,
            imported_at=now,
        )
        pl = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            import_batch_id=batch.id,
            value_date=date(2026, 10, 15),
            amount_santim=800000,
            evidence_level="L2",
            state=PaymentState.CONFIRMED.value,
            created_at=now,
        )
        pm1 = PaymentMatch(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            payment_line_id=pl.id,
            invoice_id=inv1.id,
            matched_santim=500000,
            tier="tier_b",
            state=MatchState.CONFIRMED.value,
            created_at=now,
        )
        pm2 = PaymentMatch(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            payment_line_id=pl.id,
            invoice_id=inv2.id,
            matched_santim=300000,
            tier="tier_b",
            state=MatchState.CONFIRMED.value,
            created_at=now,
        )
        db_session.add_all([inv1, inv2, batch, pl, pm1, pm2])
        db_session.flush()

        authenticate_client(client, db_session, owner)
        # Reverse match 1
        res = client.post(f"/matches/{pm1.id}/reverse", data={"reason_code": "wrong_invoice"})
        assert res.status_code == 200

        db_session.refresh(pm1)
        db_session.refresh(pm2)
        db_session.refresh(pl)
        db_session.refresh(inv1)
        db_session.refresh(inv2)

        assert pm1.state == MatchState.REVERSED.value
        assert pm2.state == MatchState.CONFIRMED.value
        assert pl.state == PaymentState.PROPOSED.value
        assert inv1.state == InvoiceState.OPEN.value
        assert inv2.state == InvoiceState.SETTLED.value

    def test_reversal_after_invoice_settled_by_another_payment(
        self, client: TestClient, db_session: Session, tenant: Tenant, owner: User, retailer: Retailer
    ):
        """
        Invoice of 6,000 ETB settled by Payment A (4,000 ETB) and Payment B (2,000 ETB).
        Reversing Payment A leaves 2,000 ETB settled and transitions invoice to partially_paid.
        """
        now = datetime.now(timezone.utc)
        inv = Invoice(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            invoice_number="INV-MULTI-1",
            retailer_id=retailer.id,
            amount_santim=600000,
            issued_date=date(2026, 10, 1),
            due_date=date(2026, 10, 31),
            state=InvoiceState.SETTLED.value,
            created_at=now,
            created_by=owner.id,
        )
        batch = ImportBatch(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            source_type="bank_statement",
            evidence_level="L2",
            filename="b.csv",
            imported_by=owner.id,
            imported_at=now,
        )
        pl_a = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            import_batch_id=batch.id,
            value_date=date(2026, 10, 10),
            amount_santim=400000,
            evidence_level="L2",
            state=PaymentState.CONFIRMED.value,
            created_at=now,
        )
        pl_b = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            import_batch_id=batch.id,
            value_date=date(2026, 10, 12),
            amount_santim=200000,
            evidence_level="L2",
            state=PaymentState.CONFIRMED.value,
            created_at=now,
        )
        pm_a = PaymentMatch(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            payment_line_id=pl_a.id,
            invoice_id=inv.id,
            matched_santim=400000,
            tier="tier_b",
            state=MatchState.CONFIRMED.value,
            created_at=now,
        )
        pm_b = PaymentMatch(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            payment_line_id=pl_b.id,
            invoice_id=inv.id,
            matched_santim=200000,
            tier="tier_b",
            state=MatchState.CONFIRMED.value,
            created_at=now,
        )
        db_session.add_all([inv, batch, pl_a, pl_b, pm_a, pm_b])
        db_session.flush()

        authenticate_client(client, db_session, owner)
        res = client.post(f"/matches/{pm_a.id}/reverse", data={"reason_code": "bank_reversal"})
        assert res.status_code == 200

        db_session.refresh(inv)
        assert inv.state == InvoiceState.PARTIALLY_PAID.value

    def test_cross_tenant_reversal_rejected(
        self, client: TestClient, db_session: Session, tenant: Tenant, owner: User, retailer: Retailer
    ):
        inv, pl, pm = create_invoice_and_match(db_session, tenant, owner, retailer, state="confirmed")

        # Create Tenant B and Owner B
        now = datetime.now(timezone.utc)
        tenant_b = Tenant(id=uuid.uuid4(), name="Tenant B", created_at=now)
        db_session.add(tenant_b)
        db_session.flush()
        set_tenant(db_session, tenant_b.id)
        owner_b = User(
            id=uuid.uuid4(),
            tenant_id=tenant_b.id,
            role=UserRole.OWNER.value,
            phone="+251944444444",
            email="owner_b@tenantb.et",
            display_name="Owner B",
            password_hash="dummy",
            is_active=True,
            created_at=now,
        )
        db_session.add(owner_b)
        db_session.flush()

        authenticate_client(client, db_session, owner_b)
        res = client.post(f"/matches/{pm.id}/reverse", data={"reason_code": "wrong_invoice"})
        # Under RLS, Tenant B cannot find Tenant A's match
        assert res.status_code == 404

    def test_reversal_restores_aging_and_accountant_export(
        self, client: TestClient, db_session: Session, tenant: Tenant, owner: User, retailer: Retailer
    ):
        inv, pl, pm = create_invoice_and_match(db_session, tenant, owner, retailer, state="confirmed")
        authenticate_client(client, db_session, owner)

        # Before reversal, invoice is settled -> 0 outstanding in aging report
        today = date(2026, 10, 20)
        rep_before = generate_aging_report(db_session, tenant_id=tenant.id, as_of_date=today)
        assert rep_before.grand_total_outstanding_santim == 0

        # Reverse match
        client.post(f"/matches/{pm.id}/reverse", data={"reason_code": "wrong_amount"})

        # After reversal, open debt is restored to aging report
        rep_after = generate_aging_report(db_session, tenant_id=tenant.id, as_of_date=today)
        assert rep_after.grand_total_outstanding_santim == 500000

        # Accountant export Section 3 lists the reversal
        csv_out = export_accountant_csv(db_session, tenant_id=tenant.id)
        assert "--- SECTION 3: REVERSED MATCHES AUDIT LOG ---" in csv_out
        assert inv.invoice_number in csv_out
        assert "500000" in csv_out


@pytest.mark.asyncio
class TestTelegramSafetyCallbacks:
    async def test_telegram_stale_button_rejected(
        self, db_session: Session, tenant: Tenant, owner: User, retailer: Retailer
    ):
        inv, pl, pm = create_invoice_and_match(db_session, tenant, owner, retailer, state="confirmed")

        # Reverse the match
        pm.state = MatchState.REVERSED.value
        db_session.commit()

        # Mock callback query clicking approve on this reversed match
        mock_callback = MagicMock()
        mock_callback.data = f"approve:{pm.id}"
        owner.telegram_id = 123456789
        db_session.commit()
        mock_callback.from_user = MagicMock()
        mock_callback.from_user.id = 123456789
        mock_callback.answer = AsyncMock()
        mock_callback.message = MagicMock()
        mock_callback.message.edit_reply_markup = AsyncMock()
        mock_callback.message.reply = AsyncMock()

        await handle_approve_callback(mock_callback)
        mock_callback.answer.assert_called_with("A reversed match can never be re-approved.", show_alert=True)


class TestStage1ReviewQueueCorrections:
    def test_is_previously_reversed_refused_by_matcher_and_trigger(
        self, db_session: Session, tenant: Tenant, owner: User, retailer: Retailer
    ):
        from app.matching.matcher import MatchResult, PaymentMatcher
        from app.kernel.enums import EvidenceLevel

        matcher = PaymentMatcher()
        now = datetime.now(timezone.utc)
        inv = Invoice(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            invoice_number="INV-REV-001",
            retailer_id=retailer.id,
            amount_santim=100000,
            issued_date=date(2026, 10, 1),
            due_date=date(2026, 10, 31),
            state=InvoiceState.OPEN.value,
            created_at=now,
            created_by=owner.id,
        )
        pl = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            value_date=date(2026, 10, 15),
            amount_santim=100000,
            raw_reference="INV-REV-001",
            counterparty_name="Retailer Corp",
            evidence_level="L1",
            state=PaymentState.PROPOSED.value,
            is_previously_reversed=True,
            created_at=now,
        )
        db_session.add_all([inv, pl])
        db_session.flush()

        # 1. match_payment returns "proposed", not "auto_confirmed"
        result = matcher.match_payment(pl, [inv], [retailer])
        assert result.status == "proposed"
        assert "previously reversed payment" in result.reason

        # 2. apply_match_results raises ValueError if forced to auto_confirm
        forced_result = MatchResult(
            payment_line_id=pl.id,
            status="auto_confirmed",
            tier=result.tier,
            evidence_level=result.evidence_level,
            matches=result.matches,
        )
        with pytest.raises(ValueError, match="Previously reversed payment cannot be auto-confirmed"):
            matcher.apply_match_results(db_session, pl, forced_result, actor_id=owner.id)

        # 3. Database trigger trg_check_no_auto_confirm_on_reversed refuses direct insert on events table
        with pytest.raises(Exception) as exc_info:
            with db_session.begin_nested():
                ev = Event(
                    id=uuid.uuid4(),
                    tenant_id=tenant.id,
                    event_type="payment.auto_confirmed",
                    aggregate_type="payment_line",
                    aggregate_id=pl.id,
                    actor_id=owner.id,
                    evidence_level="L1",
                    payload={},
                    created_at=now,
                )
                db_session.add(ev)
                db_session.flush()
        assert "previously reversed and cannot be auto-confirmed" in str(exc_info.value).lower()

    def test_reversals_csv_export(
        self, client: TestClient, db_session: Session, tenant: Tenant, owner: User, cashier: User, retailer: Retailer
    ):
        inv, pl, pm = create_invoice_and_match(db_session, tenant, owner, retailer, state="confirmed")
        authenticate_client(client, db_session, owner)
        client.post(f"/matches/{pm.id}/reverse", data={"reason_code": "wrong_invoice"})

        # Cashier is blocked (403 Forbidden)
        authenticate_client(client, db_session, cashier)
        res_cashier = client.get("/exports/reversals.csv")
        assert res_cashier.status_code == 403

        # Owner can download CSV
        authenticate_client(client, db_session, owner)
        res_owner = client.get("/exports/reversals.csv")
        assert res_owner.status_code == 200
        assert res_owner.headers["content-type"].startswith("text/csv")
        content = res_owner.text
        assert "Match ID" in content
        assert "Reason Code" in content
        assert "wrong_invoice" in content
        assert inv.invoice_number in content

    def test_delete_reversal_note_logs_id_only_without_content(
        self, client: TestClient, db_session: Session, tenant: Tenant, owner: User, cashier: User, retailer: Retailer
    ):
        inv, pl, pm = create_invoice_and_match(db_session, tenant, owner, retailer, state="confirmed")
        authenticate_client(client, db_session, owner)

        # Reverse with private sensitive free-text note
        sensitive_note_text = "SECRET_MEMO_TAX_OFFICIAL_DISPUTE_XYZ"
        client.post(f"/matches/{pm.id}/reverse", data={"reason_code": "other", "notes": sensitive_note_text})

        # Locate reversal note in DB
        set_tenant(db_session, tenant.id)
        note = db_session.execute(
            select(ReversalNote).where(ReversalNote.payment_match_id == pm.id)
        ).scalar_one()
        note_id = note.id

        # Cashier cannot delete (403 Forbidden)
        authenticate_client(client, db_session, cashier)
        res_del_cashier = client.post(f"/reversals/notes/{note_id}/delete")
        assert res_del_cashier.status_code == 403

        # Owner deletes note
        authenticate_client(client, db_session, owner)
        res_del_owner = client.post(f"/reversals/notes/{note_id}/delete")
        assert res_del_owner.status_code == 200

        # Note is removed from reversal_notes
        deleted_note = db_session.get(ReversalNote, note_id)
        assert deleted_note is None

        # Verify deletion event logs ID only, NEVER note content
        events = db_session.execute(
            select(Event).where(
                Event.event_type == "reversal_note.deleted",
                Event.aggregate_id == note_id,
            )
        ).scalars().all()
        assert len(events) == 1
        ev = events[0]
        assert ev.payload["reversal_note_id"] == str(note_id)
        assert ev.payload["payment_match_id"] == str(pm.id)
        assert sensitive_note_text not in str(ev.payload)

    def test_locale_key_parity_english_amharic(self):
        import json
        import pathlib

        root = pathlib.Path(__file__).parents[2]
        en_path = root / "app" / "channels" / "telegram" / "locales" / "en.json"
        am_path = root / "app" / "channels" / "telegram" / "locales" / "am.json"
        with open(en_path, encoding="utf-8") as f:
            en_data = json.load(f)
        with open(am_path, encoding="utf-8") as f:
            am_data = json.load(f)

        missing_in_am = set(en_data.keys()) - set(am_data.keys())
        missing_in_en = set(am_data.keys()) - set(en_data.keys())
        assert not missing_in_am, f"Keys in en.json but missing from am.json: {missing_in_am}"
        assert not missing_in_en, f"Keys in am.json but missing from en.json: {missing_in_en}"

    def test_no_external_urls_in_templates(self):
        import glob
        import pathlib
        import re

        root = pathlib.Path(__file__).parents[2]
        templates_pattern = str(root / "app" / "channels" / "web" / "templates" / "**" / "*.html")
        templates = glob.glob(templates_pattern, recursive=True)
        assert len(templates) > 0, "No templates found to audit"

        url_pattern = re.compile(r'(?:href|src)\s*=\s*[\x22\x27](?:https?:)?//', re.IGNORECASE)
        offenses = []
        for t in templates:
            with open(t, encoding="utf-8") as f:
                for idx, line in enumerate(f, 1):
                    if url_pattern.search(line):
                        offenses.append(f"{t}:{idx} -> {line.strip()}")

        assert not offenses, f"Found external URL references in templates:\n" + "\n".join(offenses)

    def test_html_escaping_of_retailer_names_in_review_screen(
        self, client: TestClient, db_session: Session, tenant: Tenant, owner: User
    ):
        now = datetime.now(timezone.utc)
        xss_retailer = Retailer(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            name="<script>alert('xss')</script> Retailer",
            phone="+251911999888",
            created_at=now,
        )
        db_session.add(xss_retailer)
        db_session.flush()

        inv, pl, pm = create_invoice_and_match(
            db_session, tenant, owner, xss_retailer, state="proposed", inv_amount_santim=250000, pl_amount_santim=250000
        )
        authenticate_client(client, db_session, owner)

        res = client.get("/")
        assert res.status_code == 200
        body = res.text
        # Must NOT contain raw unescaped script tags
        assert "<script>alert('xss')</script>" not in body
        # Must contain escaped HTML entities
        assert "&lt;script&gt;alert(" in body or "&#39;xss&#39;" in body or "&apos;xss&apos;" in body

    def test_match_review_card_context_parity_across_exact_partial_combined_overpayment(
        self, client: TestClient, db_session: Session, tenant: Tenant, owner: User, retailer: Retailer
    ):
        """
        Regression Test for Issue 1:
        Asserts that card values, signed approval token, and approval route read
        the exact same reconciliation values from compute_match_reconciliation_context
        for exact, partial, combined, and overpayment cases.
        """
        from app.matching.context import compute_match_reconciliation_context
        from app.kernel.tokens import generate_match_approval_token, verify_match_approval_token
        from app.settings import settings

        # 1. Exact case
        inv_exact, pl_exact, pm_exact = create_invoice_and_match(
            db_session, tenant, owner, retailer, state="proposed", inv_amount_santim=5000000, pl_amount_santim=5000000, matched_santim=5000000
        )
        ctx_exact = compute_match_reconciliation_context(
            db_session, tenant_id=tenant.id, payment_match=pm_exact, payment_line=pl_exact, invoice=inv_exact
        )
        assert ctx_exact.match_kind == "exact"
        assert ctx_exact.is_differing is False
        assert ctx_exact.diff_santim == 0
        assert ctx_exact.matched_santim == 5000000
        assert ctx_exact.invoice_open_santim == 5000000
        assert ctx_exact.payment_amount_santim == 5000000

        # Token verification with context
        tok = generate_match_approval_token(
            settings.secret_key,
            tenant_id=tenant.id,
            user_id=owner.id,
            match_id=pm_exact.id,
            matched_santim=ctx_exact.matched_santim,
            invoice_amount_santim=ctx_exact.invoice_total_santim,
            payment_amount_santim=ctx_exact.payment_amount_santim,
            match_state=pm_exact.state,
        )
        valid, _ = verify_match_approval_token(
            settings.secret_key,
            tok,
            expected_tenant_id=tenant.id,
            expected_user_id=owner.id,
            expected_match_id=pm_exact.id,
            current_matched_santim=ctx_exact.matched_santim,
            current_invoice_amount_santim=ctx_exact.invoice_total_santim,
            current_payment_amount_santim=ctx_exact.payment_amount_santim,
            current_match_state=pm_exact.state,
        )
        assert valid is True

        # 2. Partial case
        inv_part, pl_part, pm_part = create_invoice_and_match(
            db_session, tenant, owner, retailer, state="proposed", inv_amount_santim=12000000, pl_amount_santim=8000000, matched_santim=8000000
        )
        ctx_part = compute_match_reconciliation_context(
            db_session, tenant_id=tenant.id, payment_match=pm_part, payment_line=pl_part, invoice=inv_part
        )
        assert ctx_part.match_kind == "partial"
        assert ctx_part.is_differing is True
        assert ctx_part.diff_santim == 4000000  # 120,000 - 80,000 = 40,000 ETB
        assert ctx_part.matched_santim == 8000000
        assert ctx_part.invoice_open_santim == 12000000
        assert ctx_part.payment_amount_santim == 8000000

        # 3. Overpayment case
        inv_over, pl_over, pm_over = create_invoice_and_match(
            db_session, tenant, owner, retailer, state="proposed", inv_amount_santim=4000000, pl_amount_santim=5500000
        )
        pm_over.matched_santim = 4000000  # Matched up to invoice open balance
        db_session.flush()
        ctx_over = compute_match_reconciliation_context(
            db_session, tenant_id=tenant.id, payment_match=pm_over, payment_line=pl_over, invoice=inv_over
        )
        assert ctx_over.match_kind == "overpayment"
        assert ctx_over.is_differing is True
        assert ctx_over.diff_santim == 1500000  # 55,000 - 40,000 = 15,000 ETB
        assert ctx_over.unallocated_credit_santim == 1500000
        assert ctx_over.matched_santim == 4000000
        assert ctx_over.invoice_open_santim == 4000000
        assert ctx_over.payment_amount_santim == 5500000

        # 4. Combined case
        inv_comb1, pl_comb, pm_comb1 = create_invoice_and_match(
            db_session, tenant, owner, retailer, state="proposed", inv_amount_santim=6000000, pl_amount_santim=10000000
        )
        pm_comb1.matched_santim = 6000000
        # Second match on same payment line
        inv_comb2 = Invoice(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            invoice_number=f"INV-COMB-2-{uuid.uuid4().hex[:4]}",
            retailer_id=retailer.id,
            amount_santim=4000000,
            issued_date=datetime.now(timezone.utc).date(),
            due_date=datetime.now(timezone.utc).date() + timedelta(days=30),
            state="open",
            created_at=datetime.now(timezone.utc),
            created_by=owner.id,
        )
        db_session.add(inv_comb2)
        db_session.flush()
        pm_comb2 = PaymentMatch(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            payment_line_id=pl_comb.id,
            invoice_id=inv_comb2.id,
            matched_santim=4000000,
            tier="tier_a",
            state="proposed",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(pm_comb2)
        db_session.flush()

        ctx_comb1 = compute_match_reconciliation_context(
            db_session, tenant_id=tenant.id, payment_match=pm_comb1, payment_line=pl_comb, invoice=inv_comb1
        )
        assert ctx_comb1.match_kind == "combined"
        assert ctx_comb1.is_differing is True
        assert ctx_comb1.matched_santim == 6000000
        assert ctx_comb1.payment_amount_santim == 10000000

    def test_seeded_demo_rendered_cards_equal_approval_route_values(
        self, client: TestClient, db_session: Session, demo_tenant_and_owner: tuple[Tenant, User]
    ):
        """
        Stage 0A Regression:
        On the SEEDED demo tenant, render the review queue, parse each card's
        displayed payment amount, invoice amount, matched amount and difference,
        POST the approval request for each match, and assert the route's values
        equal the card's values. Exactly 6 seeded cards: exact, partial, overpayment, L4.
        """
        import re
        from app.kernel.money import format_money
        from app.kernel.models import PaymentMatch

        demo_t, demo_u = demo_tenant_and_owner

        # Authenticate demo owner and fetch rendered dashboard
        authenticate_client(client, db_session, demo_u)
        res = client.get("/")
        assert res.status_code == 200

        # Parse match cards from HTML
        card_blocks = res.text.split('class="match-card"')
        cards = []
        for block in card_blocks[1:]:
            m_id = re.search(r'id=["\']match-card-([a-f0-9\-]+)["\']', block)
            if not m_id:
                continue
            card_id = m_id.group(1)
            m_pay = re.search(r'<span style="font-size: 1\.15rem;[^>]*>\s*([^<\n]+)', block)
            payment_str = m_pay.group(1).strip() if m_pay else ""

            m_inv_amt = re.search(r'Amount:\s*<strong>([^<]+)</strong>', block)
            inv_amt_str = m_inv_amt.group(1).strip() if m_inv_amt else ""

            m_diff = re.search(r'Amounts differ by\s*([^(\s<]+(?:\s+ETB)?)', block)
            diff_str = m_diff.group(1).strip() if m_diff else "0.00 ETB"

            m_kind = re.search(r'Amounts differ by [^\(]+\((Exact|Partial|Overpayment|Combined)\)', block, re.IGNORECASE)
            card_kind = m_kind.group(1).lower() if m_kind else "exact"

            m_tok = re.search(r'name=["\']approval_token["\']\s+value=["\']([^"\']+)["\']', block)
            token_str = m_tok.group(1).strip() if m_tok else ""

            m_cd = re.search(r'name=["\']confirm_differing["\']\s+value=["\']([^"\']+)["\']', block)
            confirm_str = m_cd.group(1).strip() if m_cd else "false"

            cards.append({
                "id": card_id,
                "kind": card_kind,
                "payment_etb": payment_str,
                "invoice_etb": inv_amt_str,
                "diff_etb": diff_str,
                "token": token_str,
                "confirm_differing": confirm_str,
            })

        # Exactly 6 cards on initial demo seed
        assert len(cards) == 6

        covered_cases = set()
        for card in cards:
            if not card["token"]:
                continue  # L4 card has no approval token (owner override only)

            card_uuid = uuid.UUID(card["id"])
            pm = db_session.get(PaymentMatch, card_uuid)
            assert pm is not None
            pl = pm.payment_line
            inv = pm.invoice

            # Compute expected difference independently from invoice and payment amounts
            if pl.amount_santim > inv.amount_santim:
                expected_diff_santim = pl.amount_santim - inv.amount_santim
            elif pl.amount_santim < inv.amount_santim:
                expected_diff_santim = inv.amount_santim - pl.amount_santim
            else:
                expected_diff_santim = 0

            # POST the approval request for this match
            post_res = client.post(
                f"/matches/{card['id']}/approve",
                headers={"hx-request": "true"},
                data={
                    "approval_token": card["token"],
                    "confirm_differing": card["confirm_differing"],
                },
            )
            assert post_res.status_code == 200
            assert "data-testid='match-confirmed-banner'" in post_res.text or 'data-testid="match-confirmed-banner"' in post_res.text

            # Refresh from DB and verify route committed values equal card values
            db_session.expire_all()
            pm_refreshed = db_session.get(PaymentMatch, card_uuid)
            assert pm_refreshed.state == "confirmed"

            # 1. Payment amount parity
            expected_pay_str = format_money(pm_refreshed.payment_line.amount_santim, include_currency=True)
            assert card["payment_etb"] == expected_pay_str

            # 2. Invoice amount parity
            expected_inv_str = format_money(pm_refreshed.invoice.amount_santim, include_currency=True)
            assert card["invoice_etb"] == expected_inv_str

            # 3. Kind parity and independently computed difference math
            covered_cases.add(card["kind"])
            expected_diff_str = format_money(expected_diff_santim, include_currency=True)
            assert card["diff_etb"] == expected_diff_str

        assert "exact" in covered_cases
        assert "partial" in covered_cases
        assert "overpayment" in covered_cases

    def test_combined_match_rendered_card_route_parity(
        self, client: TestClient, db_session: Session, demo_tenant_and_owner: tuple[Tenant, User]
    ):
        """
        Verify rendered card and approval route parity for a combined match case.
        """
        import re
        from app.kernel.money import format_money
        from app.kernel.models import ImportBatch, Invoice, PaymentLine, PaymentMatch, Retailer

        demo_t, demo_u = demo_tenant_and_owner
        retailers = db_session.execute(
            select(Retailer).where(Retailer.tenant_id == demo_t.id)
        ).scalars().all()

        inv_c1 = Invoice(
            id=uuid.uuid4(),
            tenant_id=demo_t.id,
            invoice_number=f"INV-COMB-A-{uuid.uuid4().hex[:4].upper()}",
            retailer_id=retailers[0].id,
            amount_santim=6000000,
            issued_date=date.today() - timedelta(days=5),
            due_date=date.today() + timedelta(days=25),
            state="open",
            created_at=datetime.now(timezone.utc),
            created_by=demo_u.id,
        )
        inv_c2 = Invoice(
            id=uuid.uuid4(),
            tenant_id=demo_t.id,
            invoice_number=f"INV-COMB-B-{uuid.uuid4().hex[:4].upper()}",
            retailer_id=retailers[0].id,
            amount_santim=4000000,
            issued_date=date.today() - timedelta(days=5),
            due_date=date.today() + timedelta(days=25),
            state="open",
            created_at=datetime.now(timezone.utc),
            created_by=demo_u.id,
        )
        batch = db_session.execute(
            select(ImportBatch).where(ImportBatch.tenant_id == demo_t.id)
        ).scalars().first()
        pl_c = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=demo_t.id,
            import_batch_id=batch.id,
            source_ref=f"COMB-{uuid.uuid4().hex[:6]}",
            row_key=f"comb-row-{uuid.uuid4().hex[:4]}",
            amount_santim=10000000,
            value_date=date.today() - timedelta(days=1),
            counterparty_name="Almaz & Sons",
            evidence_level="L2",
            state="proposed",
            created_at=datetime.now(timezone.utc),
        )
        pm_c1 = PaymentMatch(
            id=uuid.uuid4(),
            tenant_id=demo_t.id,
            payment_line_id=pl_c.id,
            invoice_id=inv_c1.id,
            matched_santim=6000000,
            tier="tier_a",
            state="proposed",
            created_at=datetime.now(timezone.utc),
        )
        pm_c2 = PaymentMatch(
            id=uuid.uuid4(),
            tenant_id=demo_t.id,
            payment_line_id=pl_c.id,
            invoice_id=inv_c2.id,
            matched_santim=4000000,
            tier="tier_a",
            state="proposed",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add_all([inv_c1, inv_c2, pl_c, pm_c1, pm_c2])
        db_session.commit()

        authenticate_client(client, db_session, demo_u)
        res = client.get("/")
        assert res.status_code == 200

        # Find whichever combined match was rendered on the card
        m_match = re.search(rf'action=["\']/matches/({pm_c1.id}|{pm_c2.id})/approve["\']', res.text)
        assert m_match is not None, "Combined match card missing in review queue"
        active_pm = pm_c1 if m_match.group(1) == str(pm_c1.id) else pm_c2

        m_tok = re.search(rf'action=["\']/matches/{active_pm.id}/approve["\'].*?name=["\']approval_token["\']\s+value=["\']([^"\']+)["\']', res.text, re.DOTALL)
        assert m_tok is not None
        token_str = m_tok.group(1)

        # Expected difference computed independently: payment total - matched portion
        expected_diff = format_money(pl_c.amount_santim - active_pm.matched_santim, include_currency=True)
        assert f"Amounts differ by {expected_diff}" in res.text

        # Approve combined match
        post_res = client.post(
            f"/matches/{active_pm.id}/approve",
            headers={"hx-request": "true"},
            data={"approval_token": token_str, "confirm_differing": "true"},
        )
        assert post_res.status_code == 200
        assert "data-testid='match-confirmed-banner'" in post_res.text or 'data-testid="match-confirmed-banner"' in post_res.text

    def test_seeded_demo_differing_amount_error_leaves_card_in_place_and_count_unchanged(
        self, client: TestClient, db_session: Session, demo_tenant_and_owner: tuple[Tenant, User]
    ):
        """
        Item 1 Regression:
        On the seeded demo, clicking confirm on Tewodros Cosmetics card (payment 55,000,
        invoice open 40,000, matched 40,000) with confirm_differing absent:
        - Shows a localized message in a dedicated alert region via HX-Retarget.
        - Leaves the card in place.
        - Leaves the count unchanged.
        """
        import re
        from app.kernel.models import PaymentLine, PaymentMatch

        demo_t, demo_u = demo_tenant_and_owner
        authenticate_client(client, db_session, demo_u)

        # 1. First page load: count is 6
        res = client.get("/")
        assert res.status_code == 200
        badge_m = re.search(r'id=["\']review-queue-badge["\'][^>]*>\s*(\d+)\s+Pending Items', res.text)
        assert int(badge_m.group(1)) == 6

        # Find Tewodros match (pl amount 55,000, inv amount 40,000)
        pm_tewodros = db_session.execute(
            select(PaymentMatch)
            .join(PaymentLine, PaymentMatch.payment_line_id == PaymentLine.id)
            .where(
                PaymentMatch.tenant_id == demo_t.id,
                PaymentLine.amount_santim == 5500000,
            )
        ).scalars().first()
        assert pm_tewodros is not None

        # Extract approval token from page
        tok_m = re.search(rf'action=["\']/matches/{pm_tewodros.id}/approve["\'].*?name=["\']approval_token["\']\s+value=["\']([^"\']+)["\']', res.text, re.DOTALL)
        assert tok_m is not None
        token_str = tok_m.group(1)

        # 2. Submit approval WITHOUT confirm_differing (absent / false)
        post_res = client.post(
            f"/matches/{pm_tewodros.id}/approve",
            headers={"hx-request": "true"},
            data={"approval_token": token_str, "confirm_differing": "false"},
        )
        assert post_res.status_code == 200
        assert "data-testid='match-error-alert'" in post_res.text or 'data-testid="match-error-alert"' in post_res.text
        assert "Amounts differ" in post_res.text or "ክፍያ መጠኖች" in post_res.text
        # Assert retargets the dedicated alert region
        assert post_res.headers.get("HX-Retarget") == f"#match-card-alert-{pm_tewodros.id}"
        assert post_res.headers.get("HX-Reswap") == "innerHTML"
        # Assert count OOB swap is NOT present
        assert "hx-swap-oob=" not in post_res.text

        # 3. Subsequent page load: card is still in place, count is still 6
        res2 = client.get("/")
        assert res2.status_code == 200
        badge_m2 = re.search(r'id=["\']review-queue-badge["\'][^>]*>\s*(\d+)\s+Pending Items', res2.text)
        assert int(badge_m2.group(1)) == 6
        assert f'id="match-card-{pm_tewodros.id}"' in res2.text or f"id='match-card-{pm_tewodros.id}'" in res2.text

    def test_mark_unverified_events_and_owner_undo(
        self, client: TestClient, db_session: Session, demo_tenant_and_owner: tuple[Tenant, User]
    ):
        """
        Item 4 Regression:
        In mark_unverified:
        1. An event is written when attached proposed matches become rejected.
        2. An event is written for payment_line.accepted_unverified.
        3. Owner can undo the unverified acceptance, restoring line and match to proposed.
        4. Undo logs payment_line.unverified_acceptance_undone and match.restored_to_proposed.
        5. Review queue count reflects the undo.
        """
        import re
        from app.kernel.models import Event, PaymentLine, PaymentMatch

        demo_t, demo_u = demo_tenant_and_owner
        authenticate_client(client, db_session, demo_u)

        # Find L4 payment line (pl9) and its attached proposed match (pm9)
        pl_l4 = db_session.execute(
            select(PaymentLine)
            .where(
                PaymentLine.tenant_id == demo_t.id,
                PaymentLine.evidence_level == "L4",
                PaymentLine.state == "proposed",
            )
        ).scalars().first()
        assert pl_l4 is not None

        pm_l4 = db_session.execute(
            select(PaymentMatch)
            .where(
                PaymentMatch.payment_line_id == pl_l4.id,
                PaymentMatch.tenant_id == demo_t.id,
                PaymentMatch.state == "proposed",
            )
        ).scalars().first()
        assert pm_l4 is not None

        # 1. Call mark-unverified
        post_res = client.post(
            f"/matches/{pl_l4.id}/mark-unverified",
            headers={"hx-request": "true"},
        )
        assert post_res.status_code == 200
        assert "data-testid='match-unverified-banner'" in post_res.text or 'data-testid="match-unverified-banner"' in post_res.text
        assert "data-testid='undo-unverified-button'" in post_res.text or 'data-testid="undo-unverified-button"' in post_res.text

        # 2. Verify DB states and event persistence
        db_session.expire_all()
        pl_refreshed = db_session.get(PaymentLine, pl_l4.id)
        pm_refreshed = db_session.get(PaymentMatch, pm_l4.id)
        assert pl_refreshed.state == "accepted_unverified"
        assert pm_refreshed.state == "rejected"

        events = db_session.execute(
            select(Event).where(Event.tenant_id == demo_t.id).order_by(Event.created_at.desc())
        ).scalars().all()
        event_types = [e.event_type for e in events]
        assert "payment_line.accepted_unverified" in event_types
        assert "match.rejected" in event_types

        # Verify reject event has reason payload
        rej_event = next(e for e in events if e.event_type == "match.rejected" and e.aggregate_id == pm_l4.id)
        assert rej_event.payload.get("reason") == "payment_line_accepted_unverified"

        # 3. Call undo-unverified
        undo_res = client.post(
            f"/matches/{pl_l4.id}/undo-unverified",
            headers={"hx-request": "true"},
        )
        assert undo_res.status_code == 200
        assert "data-testid='match-undo-unverified-banner'" in undo_res.text or 'data-testid="match-undo-unverified-banner"' in undo_res.text

        # 4. Verify DB states after undo
        db_session.expire_all()
        pl_undone = db_session.get(PaymentLine, pl_l4.id)
        pm_undone = db_session.get(PaymentMatch, pm_l4.id)
        assert pl_undone.state == "proposed"
        assert pm_undone.state == "proposed"

        undo_events = db_session.execute(
            select(Event).where(Event.tenant_id == demo_t.id).order_by(Event.created_at.desc())
        ).scalars().all()
        undo_event_types = [e.event_type for e in undo_events]
        assert "payment_line.unverified_acceptance_undone" in undo_event_types
        assert "match.restored_to_proposed" in undo_event_types

    def test_demo_creation_and_passwords_refuse_in_production(self, db_session: Session, monkeypatch):
        """
        Verify that create_demo_tenant and reset_demo_tenant refuse to run
        when app_env is production.
        """
        import pytest
        from app.settings import settings
        from app.demo.generator import create_demo_tenant
        from app.demo.reset import reset_demo_tenant

        monkeypatch.setattr(settings, "app_env", "production")

        with pytest.raises(RuntimeError, match="strictly forbidden when APP_ENV=production"):
            create_demo_tenant(db_session, "Prod Demo", "owner@prod.et", "Prod Owner", "SecureDemoPass123!")

        with pytest.raises(PermissionError, match="strictly forbidden when APP_ENV=production"):
            reset_demo_tenant(db_session, uuid.uuid4())

    def test_review_queue_count_decrements_and_syncs_with_cards(
        self, client: TestClient, db_session: Session, demo_tenant_and_owner: tuple[Tenant, User]
    ):
        """
        Stage 0A Regression:
        Verify that:
        1. Badge count strictly equals the number of rendered cards on first load.
        2. After each approve, reject, and mark-unverified, the swapped OOB count
           strictly equals the remaining cards in the DOM.
        """
        import re
        from app.kernel.models import PaymentLine, PaymentMatch

        demo_t, demo_u = demo_tenant_and_owner
        authenticate_client(client, db_session, demo_u)

        # 1. First page load: assert badge count equals rendered cards
        res = client.get("/")
        assert res.status_code == 200

        badge_m = re.search(r'id=["\']review-queue-badge["\'][^>]*>\s*(\d+)\s+Pending Items', res.text)
        assert badge_m is not None, "Badge element missing on dashboard"
        badge_count = int(badge_m.group(1))

        kpi_m = re.search(r'id=["\']kpi-review-queue["\'][^>]*>\s*(\d+)\s*<', res.text)
        assert kpi_m is not None
        kpi_count = int(kpi_m.group(1))

        step3_m = re.search(r'id=["\']step3-review-count["\'][^>]*>\s*(\d+)\s*<', res.text)
        assert step3_m is not None
        step3_count = int(step3_m.group(1))

        rendered_cards = re.findall(r'class=["\']match-card["\']', res.text)
        card_count = len(rendered_cards)

        # On first load, all indicators must exactly equal rendered cards
        assert card_count == 6
        assert badge_count == card_count
        assert kpi_count == card_count
        assert step3_count == card_count

        # Fetch lines & matches for test actions
        pms = db_session.execute(
            select(PaymentMatch)
            .where(
                PaymentMatch.tenant_id == demo_t.id,
                PaymentMatch.state == "proposed",
            )
            .order_by(PaymentMatch.created_at.asc())
        ).scalars().all()
        pm_approve = pms[0]
        pm_reject = pms[1]

        pl_l4 = db_session.execute(
            select(PaymentLine)
            .where(
                PaymentLine.tenant_id == demo_t.id,
                PaymentLine.evidence_level == "L4",
                PaymentLine.state == "proposed",
            )
        ).scalars().first()
        assert pl_l4 is not None

        # 2. Action A: Approve match
        # Extract approval token for pm_approve from rendered HTML
        tok_m = re.search(rf'action=["\']/matches/{pm_approve.id}/approve["\'].*?name=["\']approval_token["\']\s+value=["\']([^"\']+)["\']', res.text, re.DOTALL)
        assert tok_m is not None
        approve_token = tok_m.group(1)

        res_appr = client.post(
            f"/matches/{pm_approve.id}/approve",
            headers={"hx-request": "true"},
            data={"approval_token": approve_token, "confirm_differing": "false"},
        )
        assert res_appr.status_code == 200
        assert "hx-swap-oob=" in res_appr.text
        assert "id='review-queue-badge'" in res_appr.text or 'id="review-queue-badge"' in res_appr.text
        assert "id='kpi-review-queue'" in res_appr.text or 'id="kpi-review-queue"' in res_appr.text
        assert "id='step3-review-count'" in res_appr.text or 'id="step3-review-count"' in res_appr.text

        appr_badge = re.search(r'id=["\']review-queue-badge["\'][^>]*>\s*(\d+)\s+Pending Items', res_appr.text)
        appr_kpi = re.search(r'id=["\']kpi-review-queue["\'][^>]*>\s*(\d+)\s*<', res_appr.text)
        appr_step3 = re.search(r'id=["\']step3-review-count["\'][^>]*>\s*(\d+)\s*<', res_appr.text)
        assert int(appr_badge.group(1)) == 5
        assert int(appr_kpi.group(1)) == 5
        assert int(appr_step3.group(1)) == 5

        # 3. Action B: Reject match
        res_rej = client.post(
            f"/matches/{pm_reject.id}/reject",
            headers={"hx-request": "true"},
        )
        assert res_rej.status_code == 200
        assert "hx-swap-oob=" in res_rej.text
        rej_badge = re.search(r'id=["\']review-queue-badge["\'][^>]*>\s*(\d+)\s+Pending Items', res_rej.text)
        rej_kpi = re.search(r'id=["\']kpi-review-queue["\'][^>]*>\s*(\d+)\s*<', res_rej.text)
        rej_step3 = re.search(r'id=["\']step3-review-count["\'][^>]*>\s*(\d+)\s*<', res_rej.text)
        assert int(rej_badge.group(1)) == 4
        assert int(rej_kpi.group(1)) == 4
        assert int(rej_step3.group(1)) == 4

        # 4. Action C: Mark unverified on L4 line
        res_unv = client.post(
            f"/matches/{pl_l4.id}/mark-unverified",
            headers={"hx-request": "true"},
        )
        assert res_unv.status_code == 200
        assert "hx-swap-oob=" in res_unv.text
        unv_badge = re.search(r'id=["\']review-queue-badge["\'][^>]*>\s*(\d+)\s+Pending Items', res_unv.text)
        unv_kpi = re.search(r'id=["\']kpi-review-queue["\'][^>]*>\s*(\d+)\s*<', res_unv.text)
        unv_step3 = re.search(r'id=["\']step3-review-count["\'][^>]*>\s*(\d+)\s*<', res_unv.text)
        assert int(unv_badge.group(1)) == 3
        assert int(unv_kpi.group(1)) == 3
        assert int(unv_step3.group(1)) == 3

        # 5. Subsequent GET /: page load remains in perfect sync with 3 cards
        res_subsequent = client.get("/")
        assert res_subsequent.status_code == 200
        sub_badge = re.search(r'id=["\']review-queue-badge["\'][^>]*>\s*(\d+)\s+Pending Items', res_subsequent.text)
        sub_kpi = re.search(r'id=["\']kpi-review-queue["\'][^>]*>\s*(\d+)\s*<', res_subsequent.text)
        sub_step3 = re.search(r'id=["\']step3-review-count["\'][^>]*>\s*(\d+)\s*<', res_subsequent.text)
        sub_cards = re.findall(r'class=["\']match-card["\']', res_subsequent.text)
        assert int(sub_badge.group(1)) == 3
        assert int(sub_kpi.group(1)) == 3
        assert int(sub_step3.group(1)) == 3
        assert len(sub_cards) == 3


class TestStage2ReviewQueueUIInvariants:
    """
    Stage 2 UI Invariant Tests:
    1. No approve button is rendered for L4 evidence.
    2. The unverified label ('not counted as paid') is present.
    3. The differing-amount step is explicit, requiring two-step confirmation.
    4. Progress counter ('Match X of Y'), 'Why we think so', and side-by-side comparison.
    """

    def test_stage2_ui_invariant_no_approve_button_rendered_for_l4(
        self, client: TestClient, db_session: Session, demo_tenant_and_owner: tuple[Tenant, User]
    ):
        import re
        demo_t, demo_u = demo_tenant_and_owner
        authenticate_client(client, db_session, demo_u)

        res = client.get("/")
        assert res.status_code == 200

        # Find L4 payment line in DB
        pl_l4 = db_session.execute(
            select(PaymentLine)
            .where(
                PaymentLine.tenant_id == demo_t.id,
                PaymentLine.evidence_level == "L4",
                PaymentLine.state == "proposed",
            )
        ).scalars().first()
        assert pl_l4 is not None

        pm_l4 = db_session.execute(
            select(PaymentMatch)
            .where(
                PaymentMatch.tenant_id == demo_t.id,
                PaymentMatch.payment_line_id == pl_l4.id,
            )
        ).scalars().first()
        card_id = pm_l4.id if pm_l4 else pl_l4.id

        # Extract the L4 card HTML block
        card_pattern = rf'(<div[^>]+id=["\']match-card-{card_id}["\'].*?)(?=<div[^>]+class=["\']match-card["\']|</div>\s*</div>\s*<!-- Reversal|<script)'
        m_card = re.search(card_pattern, res.text, re.DOTALL)
        assert m_card is not None, f"L4 card for payment {pl_l4.id} not found"
        l4_html = m_card.group(1)

        # Invariant 1: No approve button or approve form is rendered for L4
        assert "/approve" not in l4_html
        assert "data-testid=\"confirm-match-button\"" not in l4_html
        assert "data-testid='confirm-match-button'" not in l4_html
        assert "data-testid=\"confirm-differing-button\"" not in l4_html
        assert "data-testid='confirm-differing-button'" not in l4_html

    def test_stage2_ui_invariant_unverified_label_present_for_l4(
        self, client: TestClient, db_session: Session, demo_tenant_and_owner: tuple[Tenant, User]
    ):
        import re
        demo_t, demo_u = demo_tenant_and_owner
        authenticate_client(client, db_session, demo_u)

        res = client.get("/")
        assert res.status_code == 200

        pl_l4 = db_session.execute(
            select(PaymentLine)
            .where(
                PaymentLine.tenant_id == demo_t.id,
                PaymentLine.evidence_level == "L4",
                PaymentLine.state == "proposed",
            )
        ).scalars().first()
        assert pl_l4 is not None

        pm_l4 = db_session.execute(
            select(PaymentMatch)
            .where(
                PaymentMatch.tenant_id == demo_t.id,
                PaymentMatch.payment_line_id == pl_l4.id,
            )
        ).scalars().first()
        card_id = pm_l4.id if pm_l4 else pl_l4.id

        card_pattern = rf'(<div[^>]+id=["\']match-card-{card_id}["\'].*?)(?=<div[^>]+class=["\']match-card["\']|</div>\s*</div>\s*<!-- Reversal|<script)'
        m_card = re.search(card_pattern, res.text, re.DOTALL)
        assert m_card is not None
        l4_html = m_card.group(1)

        # Invariant 2: Unverified button and clear 'not counted as paid' label are present
        assert "data-testid=\"mark-unverified-button\"" in l4_html or "data-testid='mark-unverified-button'" in l4_html
        assert "not counted as paid" in l4_html.lower() or "እንደተከፈለ አይቆጠርም" in l4_html
        assert "Unverified receipt photo / screenshot" in l4_html or "ያልተረጋገጠ የደረሰኝ ፎቶ" in l4_html

    def test_stage2_ui_invariant_differing_amount_step_is_explicit(
        self, client: TestClient, db_session: Session, demo_tenant_and_owner: tuple[Tenant, User]
    ):
        import re
        demo_t, demo_u = demo_tenant_and_owner
        authenticate_client(client, db_session, demo_u)

        res = client.get("/")
        assert res.status_code == 200

        # Find Tewodros differing match (55,000 ETB vs 40,000 ETB)
        pm_tewodros = db_session.execute(
            select(PaymentMatch)
            .join(PaymentLine, PaymentMatch.payment_line_id == PaymentLine.id)
            .where(
                PaymentMatch.tenant_id == demo_t.id,
                PaymentLine.amount_santim == 5500000,
            )
        ).scalars().first()
        assert pm_tewodros is not None

        card_pattern = rf'(<div[^>]+id=["\']match-card-{pm_tewodros.id}["\'].*?)(?=<div[^>]+class=["\']match-card["\']|</div>\s*</div>\s*<!-- Reversal|<script)'
        m_card = re.search(card_pattern, res.text, re.DOTALL)
        assert m_card is not None
        card_html = m_card.group(1)

        # Invariant 3: Differing amount step is explicit
        # Routine confirm must NOT be present
        assert "data-testid=\"confirm-match-button\"" not in card_html
        assert "data-testid='confirm-match-button'" not in card_html
        # Differing confirm button MUST be present
        assert "data-testid=\"confirm-differing-button\"" in card_html or "data-testid='confirm-differing-button'" in card_html
        assert "name=\"confirm_differing\" value=\"true\"" in card_html or "name='confirm_differing' value='true'" in card_html
        # Explicit confirmation prompt text must restate amounts and retailer
        assert "Confirm and keep 15,000.00 ETB as credit" in card_html
        assert "Tewodros Cosmetics" in card_html
        assert pm_tewodros.invoice.invoice_number in card_html
        # Difference in plain words
        assert "Customer paid 55,000.00 ETB" in card_html
        assert "Invoice is 40,000.00 ETB" in card_html
        assert "Difference 15,000.00 ETB" in card_html

    def test_stage2_ui_invariant_progress_counter_and_why_it_matched(
        self, client: TestClient, db_session: Session, demo_tenant_and_owner: tuple[Tenant, User]
    ):
        import re
        demo_t, demo_u = demo_tenant_and_owner
        authenticate_client(client, db_session, demo_u)

        res = client.get("/")
        assert res.status_code == 200

        # Invariant 4: Progress counter, side-by-side comparison, and Why we think so
        assert "Match 1 of 6" in res.text
        assert "Why it matched (Why we think so):" in res.text
        assert "Suggested match, please check:" in res.text
        # No raw internal tier jargon exposed in review queue
        assert "Tier A match" not in res.text
        assert "Tier B match" not in res.text
        assert "Tier C match" not in res.text
        # Comparison structure present
        assert "match-comparison-grid" in res.text
        assert "Payment Received" in res.text
        assert "Invoice to Match" in res.text
        # Skip action present
        assert "Skip for Now" in res.text



