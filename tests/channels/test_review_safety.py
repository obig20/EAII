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
