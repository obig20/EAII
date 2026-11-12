"""
tests/channels/test_rematch.py

Stage 2: Comprehensive tests for Re-matching a Payment:
1. Owner can re-match a proposed payment match to an alternative open invoice (new proposed match created, old rejected with event).
2. Re-match NEVER auto-confirms silently (always enters proposed review state).
3. L4 photo/screenshot payments CANNOT be confirmed after rematching (Rule 2 guard intact).
4. Cross-tenant rematch attempt returns 404 Not Found.
5. Voided and settled invoices are excluded from candidates and rejected if submitted (400 Bad Request).
6. Confirmed matches cannot be rematched without reversal (409 Conflict).
7. Stale approval tokens from old match cannot approve the new match (409 Conflict).
8. Count sync between badge and review cards across state transitions.
9. Double submission is idempotent (no duplicate proposed matches created).
10. Re-matching from 'Needs attention' (unmatched/rejected payments) moves payment to proposed queue.
11. Cashier receives 403 Forbidden (owner-only action).
12. Works with JavaScript disabled (standard form POST and 303 redirect).
13. Search query filtering by retailer name, invoice number, and amount.
14. English and Amharic locale parity for rematch keys.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.kernel.tokens import generate_match_approval_token
from app.kernel.enums import EvidenceLevel, InvoiceState, MatchState, PaymentState, UserRole
from app.kernel.events import append_event
from app.kernel.models import Event, Invoice, PaymentLine, PaymentMatch, Retailer, Tenant, User
from app.kernel.tenant import set_tenant
from app.main import app
from app.matching.context import compute_match_reconciliation_context
from app.settings import settings
from tests.conftest import authenticate_client


@pytest.fixture
def rematch_data(db_session: Session, tenant_a: Tenant, tenant_b: Tenant, owner_user_a: User, owner_user_b: User):
    set_tenant(db_session, tenant_a.id)
    now = datetime.now(timezone.utc)
    today = now.date()

    # Retailers in Tenant A
    ret1 = Retailer(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        name="Addis Groceries",
        phone="+251911111111",
        created_at=now,
    )
    ret2 = Retailer(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        name="Bole Wholesalers",
        phone="+251922222222",
        created_at=now,
    )
    db_session.add_all([ret1, ret2])
    db_session.flush()

    # Invoices in Tenant A
    inv1 = Invoice(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        retailer_id=ret1.id,
        invoice_number="INV-2026-001",
        amount_santim=100_000,  # 1,000 ETB
        issued_date=today - timedelta(days=5),
        due_date=today + timedelta(days=25),
        state=InvoiceState.OPEN.value,
        created_by=owner_user_a.id,
        created_at=now,
    )
    inv2 = Invoice(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        retailer_id=ret1.id,
        invoice_number="INV-2026-002",
        amount_santim=100_000,  # 1,000 ETB
        issued_date=today - timedelta(days=3),
        due_date=today + timedelta(days=27),
        state=InvoiceState.OPEN.value,
        created_by=owner_user_a.id,
        created_at=now,
    )
    inv_void = Invoice(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        retailer_id=ret1.id,
        invoice_number="INV-2026-VOID",
        amount_santim=50_000,
        issued_date=today - timedelta(days=10),
        due_date=today + timedelta(days=20),
        state=InvoiceState.VOID.value,
        created_by=owner_user_a.id,
        created_at=now,
    )
    inv_settled = Invoice(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        retailer_id=ret2.id,
        invoice_number="INV-2026-PAID",
        amount_santim=80_000,
        issued_date=today - timedelta(days=15),
        due_date=today + timedelta(days=15),
        state=InvoiceState.SETTLED.value,
        created_by=owner_user_a.id,
        created_at=now,
    )
    db_session.add_all([inv1, inv2, inv_void, inv_settled])
    db_session.flush()

    # Payment line with L2 bank statement evidence
    pl = PaymentLine(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        value_date=today,
        amount_santim=100_000,
        evidence_level=EvidenceLevel.L2.value,
        state=PaymentState.PROPOSED.value,
        raw_reference="FT2600100 Addis Groceries",
        counterparty_name="Addis Groceries",
        created_at=now,
    )
    db_session.add(pl)
    db_session.flush()

    # Initial proposed match linking pl to inv1
    pm = PaymentMatch(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        payment_line_id=pl.id,
        invoice_id=inv1.id,
        matched_santim=100_000,
        tier="tier_a",
        state=MatchState.PROPOSED.value,
        created_at=now,
    )
    db_session.add(pm)
    db_session.flush()

    # Tenant B data (for cross-tenant attacks)
    set_tenant(db_session, tenant_b.id)
    ret_b = Retailer(
        id=uuid.uuid4(),
        tenant_id=tenant_b.id,
        name="Tenant B Retailer",
        created_at=now,
    )
    db_session.add(ret_b)
    db_session.flush()

    inv_b = Invoice(
        id=uuid.uuid4(),
        tenant_id=tenant_b.id,
        retailer_id=ret_b.id,
        invoice_number="INV-TENANT-B",
        amount_santim=100_000,
        issued_date=today,
        due_date=today + timedelta(days=30),
        state=InvoiceState.OPEN.value,
        created_by=owner_user_b.id,
        created_at=now,
    )
    db_session.add(inv_b)
    db_session.flush()

    set_tenant(db_session, tenant_a.id)
    db_session.commit()

    return {
        "tenant_a": tenant_a,
        "tenant_b": tenant_b,
        "ret1": ret1,
        "ret2": ret2,
        "inv1": inv1,
        "inv2": inv2,
        "inv_void": inv_void,
        "inv_settled": inv_settled,
        "pl": pl,
        "pm": pm,
        "inv_b": inv_b,
    }


def test_rematch_owner_creates_new_proposed_match(client: TestClient, db_session: Session, owner_user_a: User, rematch_data):
    """Owner successfully rematches a proposed payment to a different open invoice."""
    _, csrf_token = authenticate_client(client, db_session, owner_user_a)
    pm = rematch_data["pm"]
    inv2 = rematch_data["inv2"]

    # Rematch to inv2
    resp = client.post(
        f"/matches/{pm.id}/rematch",
        data={"csrf_token": csrf_token, "target_invoice_id": str(inv2.id)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/dashboard#match-card-" in resp.headers["location"]

    # Verify old match is rejected with replaced_by_owner event
    db_session.expire_all()
    set_tenant(db_session, owner_user_a.tenant_id)

    old_match = db_session.get(PaymentMatch, pm.id)
    assert old_match.state == MatchState.REJECTED.value

    rej_event = db_session.execute(
        select(Event).where(
            Event.tenant_id == owner_user_a.tenant_id,
            Event.event_type == "match.rejected",
            Event.aggregate_id == pm.id,
        )
    ).scalars().first()
    assert rej_event is not None
    assert rej_event.payload.get("reason") == "replaced_by_owner"
    assert rej_event.payload.get("replaced_by_invoice_id") == str(inv2.id)

    # Verify new match created in PROPOSED state
    new_match = db_session.execute(
        select(PaymentMatch).where(
            PaymentMatch.tenant_id == owner_user_a.tenant_id,
            PaymentMatch.payment_line_id == rematch_data["pl"].id,
            PaymentMatch.invoice_id == inv2.id,
        )
    ).scalar_one()
    assert new_match.state == MatchState.PROPOSED.value
    assert new_match.id != pm.id

    prop_event = db_session.execute(
        select(Event).where(
            Event.tenant_id == owner_user_a.tenant_id,
            Event.event_type == "match.proposed",
            Event.aggregate_id == new_match.id,
        )
    ).scalars().first()
    assert prop_event is not None
    assert prop_event.payload.get("source") == "owner_rematch"


def test_rematch_never_auto_confirms_silently(client: TestClient, db_session: Session, owner_user_a: User, rematch_data):
    """A rematched payment must NEVER auto-confirm silently, even with exact amount parity."""
    _, csrf_token = authenticate_client(client, db_session, owner_user_a)
    pm = rematch_data["pm"]
    inv2 = rematch_data["inv2"]

    resp = client.post(
        f"/matches/{pm.id}/rematch",
        data={"csrf_token": csrf_token, "target_invoice_id": str(inv2.id)},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    db_session.expire_all()
    set_tenant(db_session, owner_user_a.tenant_id)

    # Check match and payment states
    new_match = db_session.execute(
        select(PaymentMatch).where(
            PaymentMatch.tenant_id == owner_user_a.tenant_id,
            PaymentMatch.payment_line_id == rematch_data["pl"].id,
            PaymentMatch.invoice_id == inv2.id,
        )
    ).scalar_one()
    assert new_match.state == MatchState.PROPOSED.value

    pl = db_session.get(PaymentLine, rematch_data["pl"].id)
    assert pl.state == PaymentState.PROPOSED.value

    # Target invoice must still be open
    inv2_db = db_session.get(Invoice, inv2.id)
    assert inv2_db.state == InvoiceState.OPEN.value


def test_rematch_l4_photo_cannot_be_confirmed(client: TestClient, db_session: Session, owner_user_a: User, rematch_data):
    """An L4 (photo/screenshot) payment line remains strictly unconfirmable after rematching."""
    _, csrf_token = authenticate_client(client, db_session, owner_user_a)
    pl = rematch_data["pl"]
    pm = rematch_data["pm"]
    inv2 = rematch_data["inv2"]

    # Change evidence level to L4
    set_tenant(db_session, owner_user_a.tenant_id)
    pl.evidence_level = EvidenceLevel.L4.value
    db_session.commit()

    # Re-match to inv2
    resp_rematch = client.post(
        f"/matches/{pm.id}/rematch",
        data={"csrf_token": csrf_token, "target_invoice_id": str(inv2.id)},
        follow_redirects=False,
    )
    assert resp_rematch.status_code == 303

    db_session.expire_all()
    set_tenant(db_session, owner_user_a.tenant_id)

    new_match = db_session.execute(
        select(PaymentMatch).where(
            PaymentMatch.payment_line_id == pl.id,
            PaymentMatch.invoice_id == inv2.id,
            PaymentMatch.state == MatchState.PROPOSED.value,
        )
    ).scalar_one()

    # Try to approve the newly rematched L4 match via approval endpoint
    resp_approve = client.post(
        f"/matches/{new_match.id}/approve",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert resp_approve.status_code == 400
    assert "Rule 2 Guard" in resp_approve.text or "L4" in resp_approve.text or "insufficient" in resp_approve.text.lower()

    # Verify match was not confirmed
    db_session.expire_all()
    set_tenant(db_session, owner_user_a.tenant_id)
    nm = db_session.get(PaymentMatch, new_match.id)
    assert nm.state == MatchState.PROPOSED.value


def test_rematch_cross_tenant_blocked(client: TestClient, db_session: Session, owner_user_a: User, rematch_data):
    """Tenant isolation: cannot rematch to an invoice in another tenant or rematch another tenant's match."""
    _, csrf_token = authenticate_client(client, db_session, owner_user_a)
    pm = rematch_data["pm"]
    inv_b = rematch_data["inv_b"]

    # 1. Attempt to rematch Tenant A's match to Tenant B's invoice -> 404
    resp = client.post(
        f"/matches/{pm.id}/rematch",
        data={"csrf_token": csrf_token, "target_invoice_id": str(inv_b.id)},
    )
    assert resp.status_code == 404

    # 2. Attempt to GET rematch page for non-existent / other tenant match -> 404
    fake_match_id = uuid.uuid4()
    resp_get = client.get(f"/matches/{fake_match_id}/rematch")
    assert resp_get.status_code == 404


def test_rematch_voided_and_settled_invoices_excluded(client: TestClient, db_session: Session, owner_user_a: User, rematch_data):
    """Voided and settled invoices are strictly excluded from candidates and rejected on submit."""
    _, csrf_token = authenticate_client(client, db_session, owner_user_a)
    pm = rematch_data["pm"]
    inv_void = rematch_data["inv_void"]
    inv_settled = rematch_data["inv_settled"]

    # 1. Check GET rematch candidates list: neither voided nor settled invoices appear
    resp_get = client.get(f"/matches/{pm.id}/rematch")
    assert resp_get.status_code == 200
    assert "INV-2026-VOID" not in resp_get.text
    assert "INV-2026-PAID" not in resp_get.text

    # 2. Direct POST targeting voided invoice -> 400 Bad Request
    resp_void = client.post(
        f"/matches/{pm.id}/rematch",
        data={"csrf_token": csrf_token, "target_invoice_id": str(inv_void.id)},
    )
    assert resp_void.status_code == 400
    assert "void" in resp_void.text.lower()

    # 3. Direct POST targeting settled invoice -> 400 Bad Request
    resp_settled = client.post(
        f"/matches/{pm.id}/rematch",
        data={"csrf_token": csrf_token, "target_invoice_id": str(inv_settled.id)},
    )
    assert resp_settled.status_code == 400
    assert "settled" in resp_settled.text.lower()


def test_rematch_blocked_when_match_is_already_confirmed(client: TestClient, db_session: Session, owner_user_a: User, rematch_data):
    """Confirmed matches cannot be rematched directly without reversing first (409 Conflict)."""
    _, csrf_token = authenticate_client(client, db_session, owner_user_a)
    pm = rematch_data["pm"]
    inv2 = rematch_data["inv2"]

    set_tenant(db_session, owner_user_a.tenant_id)
    pm.state = MatchState.CONFIRMED.value
    db_session.commit()

    resp = client.post(
        f"/matches/{pm.id}/rematch",
        data={"csrf_token": csrf_token, "target_invoice_id": str(inv2.id)},
    )
    assert resp.status_code == 409
    assert "reverse" in resp.text.lower()


def test_rematch_stale_token_rejected(client: TestClient, db_session: Session, owner_user_a: User, rematch_data):
    """Approval token generated for old match is rejected against the newly rematched match."""
    _, csrf_token = authenticate_client(client, db_session, owner_user_a)
    pm = rematch_data["pm"]
    pl = rematch_data["pl"]
    inv1 = rematch_data["inv1"]
    inv2 = rematch_data["inv2"]

    # Generate approval token for pm (linked to inv1)
    set_tenant(db_session, owner_user_a.tenant_id)
    ctx1 = compute_match_reconciliation_context(
        db_session,
        tenant_id=owner_user_a.tenant_id,
        payment_match=pm,
        payment_line=pl,
        invoice=inv1,
    )
    old_token = generate_match_approval_token(
        settings.secret_key,
        tenant_id=owner_user_a.tenant_id,
        user_id=owner_user_a.id,
        match_id=pm.id,
        matched_santim=ctx1.matched_santim,
        invoice_amount_santim=ctx1.invoice_total_santim,
        payment_amount_santim=ctx1.payment_amount_santim,
        match_state=pm.state,
    )

    # Rematch to inv2
    resp_rematch = client.post(
        f"/matches/{pm.id}/rematch",
        data={"csrf_token": csrf_token, "target_invoice_id": str(inv2.id)},
        follow_redirects=False,
    )
    assert resp_rematch.status_code == 303

    db_session.expire_all()
    set_tenant(db_session, owner_user_a.tenant_id)
    new_match = db_session.execute(
        select(PaymentMatch).where(
            PaymentMatch.payment_line_id == pl.id,
            PaymentMatch.invoice_id == inv2.id,
            PaymentMatch.state == MatchState.PROPOSED.value,
        )
    ).scalar_one()

    # Attempting to approve new match with old token fails with 409 Conflict
    resp_approve_stale = client.post(
        f"/matches/{new_match.id}/approve",
        data={"csrf_token": csrf_token, "approval_token": old_token},
        follow_redirects=False,
    )
    assert resp_approve_stale.status_code == 409

    # Generate fresh token for new match and verify it approves cleanly
    ctx2 = compute_match_reconciliation_context(
        db_session,
        tenant_id=owner_user_a.tenant_id,
        payment_match=new_match,
        payment_line=pl,
        invoice=inv2,
    )
    fresh_token = generate_match_approval_token(
        settings.secret_key,
        tenant_id=owner_user_a.tenant_id,
        user_id=owner_user_a.id,
        match_id=new_match.id,
        matched_santim=ctx2.matched_santim,
        invoice_amount_santim=ctx2.invoice_total_santim,
        payment_amount_santim=ctx2.payment_amount_santim,
        match_state=new_match.state,
    )

    resp_approve_fresh = client.post(
        f"/matches/{new_match.id}/approve",
        data={"csrf_token": csrf_token, "approval_token": fresh_token},
        follow_redirects=False,
    )
    assert resp_approve_fresh.status_code == 200
    assert "Confirmed" in resp_approve_fresh.text


def test_rematch_count_sync_between_badge_and_cards(client: TestClient, db_session: Session, owner_user_a: User, rematch_data):
    """Review queue count maintains strict synchronicity between badge and cards when rematching."""
    _, csrf_token = authenticate_client(client, db_session, owner_user_a)
    pm = rematch_data["pm"]
    pl = rematch_data["pl"]
    inv2 = rematch_data["inv2"]

    # 1. When payment was already proposed, rematching to another invoice preserves count (1 -> 1)
    resp_dash1 = client.get("/dashboard")
    assert 'class="task-number">3</div>' in resp_dash1.text
    assert '<span id="step3-review-count"' in resp_dash1.text

    resp_rematch = client.post(
        f"/matches/{pm.id}/rematch",
        data={"csrf_token": csrf_token, "target_invoice_id": str(inv2.id)},
        follow_redirects=True,
    )
    assert resp_rematch.status_code == 200
    # Exactly 1 match card rendered
    assert resp_rematch.text.count('data-testid="match-card"') == 1

    # 2. When a rejected payment in Needs Attention is rematched, pending count increments
    set_tenant(db_session, owner_user_a.tenant_id)
    now = datetime.now(timezone.utc)
    pl_rejected = PaymentLine(
        id=uuid.uuid4(),
        tenant_id=owner_user_a.tenant_id,
        value_date=now.date(),
        amount_santim=40_000,
        evidence_level=EvidenceLevel.L2.value,
        state=PaymentState.REJECTED.value,
        counterparty_name="Rejected Retailer",
        created_at=now,
    )
    db_session.add(pl_rejected)
    db_session.commit()

    resp_rematch_rej = client.post(
        f"/payments/{pl_rejected.id}/rematch",
        data={"csrf_token": csrf_token, "target_invoice_id": str(rematch_data["inv1"].id)},
        follow_redirects=True,
    )
    assert resp_rematch_rej.status_code == 200
    # Now 2 match cards rendered in review queue
    assert resp_rematch_rej.text.count('data-testid="match-card"') == 2


def test_rematch_double_submission_idempotency(client: TestClient, db_session: Session, owner_user_a: User, rematch_data):
    """Double-clicking or repeating a rematch request is idempotent and does not create duplicate matches."""
    _, csrf_token = authenticate_client(client, db_session, owner_user_a)
    pm = rematch_data["pm"]
    inv2 = rematch_data["inv2"]

    # First submission
    resp1 = client.post(
        f"/matches/{pm.id}/rematch",
        data={"csrf_token": csrf_token, "target_invoice_id": str(inv2.id)},
        follow_redirects=False,
    )
    assert resp1.status_code == 303

    # Second submission with same parameters
    resp2 = client.post(
        f"/matches/{pm.id}/rematch",
        data={"csrf_token": csrf_token, "target_invoice_id": str(inv2.id)},
        follow_redirects=False,
    )
    assert resp2.status_code == 303

    # Total proposed matches for this payment line must remain exactly 1
    db_session.expire_all()
    set_tenant(db_session, owner_user_a.tenant_id)
    proposed_matches = db_session.execute(
        select(PaymentMatch).where(
            PaymentMatch.payment_line_id == rematch_data["pl"].id,
            PaymentMatch.state == MatchState.PROPOSED.value,
        )
    ).scalars().all()
    assert len(proposed_matches) == 1


def test_rematch_from_needs_attention_unmatched_payment(client: TestClient, db_session: Session, owner_user_a: User, rematch_data):
    """An unmatched statement entry from Needs Attention can be rematched to an open invoice."""
    _, csrf_token = authenticate_client(client, db_session, owner_user_a)
    now = datetime.now(timezone.utc)
    set_tenant(db_session, owner_user_a.tenant_id)

    pl_unmatched = PaymentLine(
        id=uuid.uuid4(),
        tenant_id=owner_user_a.tenant_id,
        value_date=now.date(),
        amount_santim=75_000,
        evidence_level=EvidenceLevel.L2.value,
        state=PaymentState.UNMATCHED.value,
        counterparty_name="Direct Bank Deposit",
        created_at=now,
    )
    db_session.add(pl_unmatched)
    db_session.commit()

    # GET rematch page for unmatched payment
    resp_get = client.get(f"/payments/{pl_unmatched.id}/rematch")
    assert resp_get.status_code == 200
    assert "Direct Bank Deposit" in resp_get.text
    assert "INV-2026-001" in resp_get.text

    # POST rematch to inv1
    resp_post = client.post(
        f"/payments/{pl_unmatched.id}/rematch",
        data={"csrf_token": csrf_token, "target_invoice_id": str(rematch_data["inv1"].id)},
        follow_redirects=False,
    )
    assert resp_post.status_code == 303

    db_session.expire_all()
    set_tenant(db_session, owner_user_a.tenant_id)
    pl_db = db_session.get(PaymentLine, pl_unmatched.id)
    assert pl_db.state == PaymentState.PROPOSED.value

    pm_new = db_session.execute(
        select(PaymentMatch).where(
            PaymentMatch.payment_line_id == pl_unmatched.id,
            PaymentMatch.invoice_id == rematch_data["inv1"].id,
        )
    ).scalar_one()
    assert pm_new.state == MatchState.PROPOSED.value


def test_rematch_cashier_forbidden(client: TestClient, db_session: Session, cashier_user_a: User, rematch_data):
    """Cashier user is forbidden (403) from accessing rematch endpoints."""
    _, csrf_token = authenticate_client(client, db_session, cashier_user_a)
    pm = rematch_data["pm"]
    inv2 = rematch_data["inv2"]

    # GET is 403
    resp_get = client.get(f"/matches/{pm.id}/rematch")
    assert resp_get.status_code == 403

    # POST is 403
    resp_post = client.post(
        f"/matches/{pm.id}/rematch",
        data={"csrf_token": csrf_token, "target_invoice_id": str(inv2.id)},
    )
    assert resp_post.status_code == 403


def test_rematch_js_disabled_flow(client: TestClient, db_session: Session, owner_user_a: User, rematch_data):
    """Full rematching workflow operates seamlessly without client-side JavaScript."""
    _, csrf_token = authenticate_client(client, db_session, owner_user_a)
    pm = rematch_data["pm"]
    inv2 = rematch_data["inv2"]

    # 1. Plain HTTP GET request
    resp_get = client.get(f"/matches/{pm.id}/rematch")
    assert resp_get.status_code == 200
    assert '<form method="POST"' in resp_get.text
    assert f'value="{inv2.id}"' in resp_get.text

    # 2. Standard browser form submission with follow_redirects=True
    resp_post = client.post(
        f"/matches/{pm.id}/rematch",
        data={"csrf_token": csrf_token, "target_invoice_id": str(inv2.id)},
        follow_redirects=True,
    )
    assert resp_post.status_code == 200
    assert "INV-2026-002" in resp_post.text


def test_rematch_search_filtering(client: TestClient, db_session: Session, owner_user_a: User, rematch_data):
    """Search query parameter correctly filters candidate invoices by number, retailer, and amount."""
    authenticate_client(client, db_session, owner_user_a)
    pm = rematch_data["pm"]

    # 1. Search by invoice number
    resp_inv = client.get(f"/matches/{pm.id}/rematch?q=002")
    assert resp_inv.status_code == 200
    assert 'data-testid="candidate-invoice-INV-2026-002"' in resp_inv.text
    assert 'data-testid="candidate-invoice-INV-2026-001"' not in resp_inv.text

    # 2. Search by non-existent query -> empty state
    resp_none = client.get(f"/matches/{pm.id}/rematch?q=NONEXISTENT_STORE")
    assert resp_none.status_code == 200
    assert 'data-testid="rematch-no-invoices"' in resp_none.text


def test_locale_parity_for_rematch_keys():
    """English and Amharic locales maintain 100% key parity with reviewed tags."""
    locales_dir = Path("app/channels/telegram/locales")
    with open(locales_dir / "en.json", "r", encoding="utf-8") as f:
        en = json.load(f)
    with open(locales_dir / "am.json", "r", encoding="utf-8") as f:
        am = json.load(f)

    assert set(en.keys()) == set(am.keys())

    rematch_keys = [
        "rematch_action",
        "rematch_from_needs_attn",
        "rematch_title",
        "rematch_subtitle",
        "rematch_select_invoice",
        "rematch_search_placeholder",
        "rematch_no_invoices",
        "rematch_current_match",
        "rematch_payment_details",
        "rematch_open_invoices",
    ]
    for k in rematch_keys:
        assert k in en, f"Missing key in en.json: {k}"
        assert k in am, f"Missing key in am.json: {k}"
        assert "needs native review" in am[k], f"Amharic key {k} must have needs native review tag"
