"""
tests/channels/test_void_sales.py

Stage 1: Comprehensive tests for Voiding a Credit Sale:
1. Cashier receives 403 Forbidden (owner-only action).
2. Cross-tenant void attempt returns 404 Not Found.
3. Double void returns 409 Conflict.
4. Blocked while any CONFIRMED match is attached (returns 409 Conflict).
5. Proposed matches to the voided invoice are rejected with events, and payment returns to queue.
6. Voided invoices are excluded from aging report and morning brief.
7. Voided invoices are excluded from retailer confirmation balance links.
8. Voided invoices are listed separately in exports (sales, accountant, voided-sales).
9. Voided invoice numbers are counted as used in sequence-gap detection (no false gap).
10. Number reuse is permitted ONLY via the Void & Re-enter flow, linked via replaces_invoice_id.
11. Full locale parity between en.json and am.json.
12. HTML escaping of special characters in confirmation prompts.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.channels.web.routes import get_session
from app.kernel.enums import EvidenceLevel, InvoiceState, MatchState, PaymentState, UserRole, VoidReasonCode
from app.kernel.events import append_event
from app.kernel.models import (
    Event,
    Invoice,
    InvoiceVoidNote,
    PaymentLine,
    PaymentMatch,
    Retailer,
    Tenant,
    User,
)
from app.kernel.retailer_confirmation import create_retailer_confirmation_token, validate_confirmation_token
from app.kernel.sales import (
    AlreadyVoidedError,
    DuplicateInvoiceError,
    InvoiceHasConfirmedMatchesError,
    InvoiceNotFoundError,
    detect_sequence_gaps,
    record_credit_sale,
    void_credit_sale,
)
from app.kernel.tenant import set_tenant
from app.main import app
from app.reports.aging import generate_aging_report
from app.reports.exports import export_accountant_csv, export_sales_csv, export_voided_sales_csv
from app.reports.morning_brief import generate_morning_brief_text
from tests.conftest import authenticate_client


@pytest.fixture
def client(db_session: Session):
    def _override_get_session():
        yield db_session

    app.dependency_overrides[get_session] = _override_get_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def stage1_setup(
    db_session: Session,
    tenant_a: Tenant,
    tenant_b: Tenant,
    owner_user_a: User,
    cashier_user_a: User,
    owner_user_b: User,
):
    """Sets up tenant fixtures using conftest standard fixtures with RLS established."""
    now = datetime.now(timezone.utc)
    today = now.date()

    set_tenant(db_session, tenant_a.id)
    ret1 = Retailer(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        name="Abyssinia Retail",
        phone="+251933333333",
        created_at=now,
    )
    db_session.add(ret1)
    db_session.flush()

    inv_normal, _ = record_credit_sale(
        db_session,
        tenant_id=tenant_a.id,
        user_id=owner_user_a.id,
        invoice_number="INV-101",
        retailer_id=ret1.id,
        amount_santim=50_000_00,
        issued_date=today - timedelta(days=5),
        due_date=today + timedelta(days=25),
    )

    inv_gap2, _ = record_credit_sale(
        db_session,
        tenant_id=tenant_a.id,
        user_id=owner_user_a.id,
        invoice_number="INV-102",
        retailer_id=ret1.id,
        amount_santim=30_000_00,
        issued_date=today - timedelta(days=4),
        due_date=today + timedelta(days=26),
    )

    set_tenant(db_session, tenant_b.id)
    ret2 = Retailer(
        id=uuid.uuid4(),
        tenant_id=tenant_b.id,
        name="Foreign Retail",
        phone="+251944444444",
        created_at=now,
    )
    db_session.add(ret2)
    db_session.flush()

    inv_t2, _ = record_credit_sale(
        db_session,
        tenant_id=tenant_b.id,
        user_id=owner_user_b.id,
        invoice_number="INV-T2-01",
        retailer_id=ret2.id,
        amount_santim=10_000_00,
        issued_date=today,
        due_date=today + timedelta(days=30),
    )

    db_session.commit()

    return {
        "tenant1": tenant_a,
        "tenant2": tenant_b,
        "owner": owner_user_a,
        "cashier": cashier_user_a,
        "ret1": ret1,
        "ret2": ret2,
        "inv_normal": inv_normal,
        "inv_gap2": inv_gap2,
        "inv_t2": inv_t2,
    }


def test_void_sale_owner_success(client: TestClient, db_session: Session, stage1_setup):
    """Owner successfully voids an invoice with reason code and optional notes."""
    data = stage1_setup
    authenticate_client(client, db_session, data["owner"])

    resp = client.post(
        f"/invoices/{data['inv_normal'].id}/void",
        data={"reason_code": "wrong_amount", "notes": "Entered 50k instead of 5k"},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    # Verify database state
    set_tenant(db_session, data["tenant1"].id)
    inv = db_session.get(Invoice, data["inv_normal"].id)
    assert inv.state == InvoiceState.VOID.value

    # Verify separate notes table
    note = db_session.execute(
        select(InvoiceVoidNote).where(InvoiceVoidNote.invoice_id == inv.id)
    ).scalar_one()
    assert note.reason_code == "wrong_amount"
    assert note.notes == "Entered 50k instead of 5k"
    assert note.created_by == data["owner"].id

    # Verify compensating event contains NO free text notes
    ev = db_session.execute(
        select(Event).where(
            Event.tenant_id == data["tenant1"].id,
            Event.event_type == "invoice.voided",
            Event.aggregate_id == inv.id,
        )
    ).scalar_one()
    assert ev.payload["reason_code"] == "wrong_amount"
    assert ev.payload["original_amount_santim"] == 50_000_00
    assert "notes" not in ev.payload


def test_void_sale_cashier_403(client: TestClient, db_session: Session, stage1_setup):
    """Cashiers are strictly forbidden from voiding invoices (HTTP 403)."""
    data = stage1_setup
    authenticate_client(client, db_session, data["cashier"])

    resp = client.post(
        f"/invoices/{data['inv_normal'].id}/void",
        data={"reason_code": "wrong_amount"},
    )
    assert resp.status_code == 403


def test_void_sale_cross_tenant_404(client: TestClient, db_session: Session, stage1_setup):
    """Tenant A owner cannot void Tenant B's invoice (returns 404)."""
    data = stage1_setup
    authenticate_client(client, db_session, data["owner"])

    resp = client.post(
        f"/invoices/{data['inv_t2'].id}/void",
        data={"reason_code": "wrong_amount"},
    )
    assert resp.status_code == 404


def test_void_sale_double_void_409(client: TestClient, db_session: Session, stage1_setup):
    """Attempting to void an already-voided invoice returns 409 Conflict."""
    data = stage1_setup
    authenticate_client(client, db_session, data["owner"])

    # First void succeeds
    resp1 = client.post(
        f"/invoices/{data['inv_normal'].id}/void",
        data={"reason_code": "wrong_amount"},
    )
    assert resp1.status_code in (200, 303)

    # Second void rejected with 409
    resp2 = client.post(
        f"/invoices/{data['inv_normal'].id}/void",
        data={"reason_code": "wrong_amount"},
    )
    assert resp2.status_code == 409
    assert "already voided" in resp2.json()["detail"].lower()


def test_void_sale_blocked_when_confirmed_match_attached(client: TestClient, db_session: Session, stage1_setup):
    """Voiding is blocked when any confirmed match is attached, directing owner to reverse first."""
    data = stage1_setup
    set_tenant(db_session, data["tenant1"].id)

    # Create a payment line and confirmed match against inv_normal
    now = datetime.now(timezone.utc)
    pl = PaymentLine(
        id=uuid.uuid4(),
        tenant_id=data["tenant1"].id,
        amount_santim=50_000_00,
        value_date=date.today(),
        evidence_level=EvidenceLevel.L2.value,
        state=PaymentState.CONFIRMED.value,
        created_at=now,
    )
    pm = PaymentMatch(
        id=uuid.uuid4(),
        tenant_id=data["tenant1"].id,
        invoice_id=data["inv_normal"].id,
        payment_line_id=pl.id,
        matched_santim=50_000_00,
        tier="tier_a",
        state=MatchState.CONFIRMED.value,
        created_at=now,
    )
    db_session.add_all([pl, pm])
    db_session.commit()

    authenticate_client(client, db_session, data["owner"])
    resp = client.post(
        f"/invoices/{data['inv_normal'].id}/void",
        data={"reason_code": "wrong_amount"},
    )
    assert resp.status_code == 409
    assert "reverse" in resp.json()["detail"].lower()


def test_void_sale_unlinks_proposed_matches(client: TestClient, db_session: Session, stage1_setup):
    """When an invoice is voided, attached proposed matches are rejected and payment returns to queue."""
    data = stage1_setup
    set_tenant(db_session, data["tenant1"].id)

    now = datetime.now(timezone.utc)
    pl = PaymentLine(
        id=uuid.uuid4(),
        tenant_id=data["tenant1"].id,
        amount_santim=50_000_00,
        value_date=date.today(),
        evidence_level=EvidenceLevel.L2.value,
        state=PaymentState.PROPOSED.value,
        created_at=now,
    )
    pm = PaymentMatch(
        id=uuid.uuid4(),
        tenant_id=data["tenant1"].id,
        invoice_id=data["inv_normal"].id,
        payment_line_id=pl.id,
        matched_santim=50_000_00,
        tier="tier_a",
        state=MatchState.PROPOSED.value,
        created_at=now,
    )
    db_session.add_all([pl, pm])
    db_session.commit()

    authenticate_client(client, db_session, data["owner"])
    resp = client.post(
        f"/invoices/{data['inv_normal'].id}/void",
        data={"reason_code": "duplicate_entry"},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    db_session.refresh(pm)
    db_session.refresh(pl)
    assert pm.state == MatchState.REJECTED.value
    assert pl.state == PaymentState.UNMATCHED.value

    # Verify event logged for match rejection
    ev = db_session.execute(
        select(Event).where(
            Event.tenant_id == data["tenant1"].id,
            Event.event_type == "match.rejected",
            Event.aggregate_id == pm.id,
        )
    ).scalar_one()
    assert ev.payload["reason_code"] == "invoice_voided"


def test_void_sale_excluded_from_aging_and_brief(db_session: Session, stage1_setup):
    """Voided sales are strictly excluded from the aging report and morning brief."""
    data = stage1_setup
    set_tenant(db_session, data["tenant1"].id)

    # Before voiding: aging has both inv_normal (50k) and inv_gap2 (30k) = 80k
    rep_before = generate_aging_report(db_session, tenant_id=data["tenant1"].id)
    assert rep_before.grand_total_outstanding_santim == 80_000_00

    # Void inv_normal
    void_credit_sale(
        db_session,
        tenant_id=data["tenant1"].id,
        user_id=data["owner"].id,
        invoice_id=data["inv_normal"].id,
        reason_code=VoidReasonCode.WRONG_AMOUNT.value,
    )
    db_session.commit()

    # After voiding: aging only has inv_gap2 (30k)
    rep_after = generate_aging_report(db_session, tenant_id=data["tenant1"].id)
    assert rep_after.grand_total_outstanding_santim == 30_000_00

    # Morning brief output excludes inv_normal
    brief = generate_morning_brief_text(db_session, tenant_id=data["tenant1"].id)
    assert "INV-101" not in brief


def test_void_sale_excluded_from_retailer_confirmation_links(db_session: Session, stage1_setup):
    """Voided sales are excluded from retailer confirmation balances and token links."""
    data = stage1_setup
    set_tenant(db_session, data["tenant1"].id)

    # Void inv_normal
    void_credit_sale(
        db_session,
        tenant_id=data["tenant1"].id,
        user_id=data["owner"].id,
        invoice_id=data["inv_normal"].id,
        reason_code=VoidReasonCode.WRONG_AMOUNT.value,
    )
    db_session.commit()

    token, raw_token = create_retailer_confirmation_token(
        db_session,
        tenant_id=data["tenant1"].id,
        retailer_id=data["ret1"].id,
    )
    db_session.commit()

    token_rec, ret, invoices = validate_confirmation_token(db_session, raw_token=raw_token)
    assert ret.name == data["ret1"].name
    inv_nums = [inv.invoice_number for inv in invoices]
    assert "INV-101" not in inv_nums
    assert "INV-102" in inv_nums


def test_void_sale_listed_separately_in_exports(db_session: Session, stage1_setup):
    """Sales and accountant exports list voided invoices in dedicated audit sections."""
    data = stage1_setup
    set_tenant(db_session, data["tenant1"].id)

    void_credit_sale(
        db_session,
        tenant_id=data["tenant1"].id,
        user_id=data["owner"].id,
        invoice_id=data["inv_normal"].id,
        reason_code=VoidReasonCode.CANCELLED_SALE.value,
    )
    db_session.commit()

    # Sales CSV Export
    sales_csv = export_sales_csv(db_session, tenant_id=data["tenant1"].id)
    assert "--- VOIDED SALES (AUDIT TRAIL) ---" in sales_csv
    assert "INV-101" in sales_csv

    # Accountant CSV Export
    acc_csv = export_accountant_csv(db_session, tenant_id=data["tenant1"].id)
    assert "--- SECTION 4: VOIDED SALES (AUDIT TRAIL) ---" in acc_csv
    assert "INV-101" in acc_csv

    # Dedicated Voided Sales CSV Export
    void_csv = export_voided_sales_csv(db_session, tenant_id=data["tenant1"].id)
    assert "INV-101" in void_csv
    assert "cancelled_sale" in void_csv
    assert "INV-102" not in void_csv  # active invoice not in voided export


def test_void_sale_counted_in_sequence_gaps(db_session: Session, stage1_setup):
    """Voided invoices are counted as used so they do not trigger a sequence gap warning."""
    data = stage1_setup
    set_tenant(db_session, data["tenant1"].id)

    # We have INV-101 and INV-102. No gap between them.
    numbers = ["INV-101", "INV-102", "INV-103"]
    assert detect_sequence_gaps(numbers) == []

    # Void INV-102
    void_credit_sale(
        db_session,
        tenant_id=data["tenant1"].id,
        user_id=data["owner"].id,
        invoice_id=data["inv_gap2"].id,
        reason_code=VoidReasonCode.DUPLICATE_ENTRY.value,
    )
    db_session.commit()

    # When INV-103 is recorded, INV-102 is still counted as used in sequence check
    inv103, gaps = record_credit_sale(
        db_session,
        tenant_id=data["tenant1"].id,
        user_id=data["owner"].id,
        invoice_number="INV-103",
        retailer_id=data["ret1"].id,
        amount_santim=20_000_00,
        issued_date=date.today(),
        due_date=date.today() + timedelta(days=30),
    )
    assert "INV-102" not in gaps


def test_void_and_reenter_flow_number_reuse(client: TestClient, db_session: Session, stage1_setup):
    """The same invoice number may be reused ONLY through the void-and-reenter flow linked to the voided one."""
    data = stage1_setup
    set_tenant(db_session, data["tenant1"].id)

    # Void INV-101
    void_credit_sale(
        db_session,
        tenant_id=data["tenant1"].id,
        user_id=data["owner"].id,
        invoice_id=data["inv_normal"].id,
        reason_code=VoidReasonCode.WRONG_AMOUNT.value,
    )
    db_session.commit()

    # 1. Attempting to record INV-101 normally without replaces_invoice_id is REJECTED
    with pytest.raises(DuplicateInvoiceError) as exc_info:
        record_credit_sale(
            db_session,
            tenant_id=data["tenant1"].id,
            user_id=data["owner"].id,
            invoice_number="INV-101",
            retailer_id=data["ret1"].id,
            amount_santim=45_000_00,
            issued_date=date.today(),
            due_date=date.today() + timedelta(days=30),
        )
    assert "belongs to a voided sale" in str(exc_info.value)

    # 2. Re-entering via the void-and-reenter endpoint and linking replaces_invoice_id SUCCEEDS
    authenticate_client(client, db_session, data["owner"])

    # GET /invoices/{id}/void-and-reenter redirects to dashboard with prefill param
    resp_get = client.get(f"/invoices/{data['inv_normal'].id}/void-and-reenter", follow_redirects=False)
    assert resp_get.status_code == 303
    assert f"reenter_invoice_id={data['inv_normal'].id}" in resp_get.headers["location"]

    # POST /sales with replaces_invoice_id succeeds
    resp_post = client.post(
        "/sales",
        data={
            "invoice_number": "INV-101",
            "retailer_id": str(data["ret1"].id),
            "amount_etb": "450.00",
            "due_date": (date.today() + timedelta(days=30)).isoformat(),
            "replaces_invoice_id": str(data["inv_normal"].id),
        },
        follow_redirects=True,
    )
    assert resp_post.status_code == 200

    # Verify the new invoice is created with replaces_invoice_id linked
    set_tenant(db_session, data["tenant1"].id)
    new_inv = db_session.execute(
        select(Invoice).where(
            Invoice.tenant_id == data["tenant1"].id,
            Invoice.invoice_number == "INV-101",
            Invoice.state == InvoiceState.OPEN.value,
        )
    ).scalar_one()
    assert new_inv.amount_santim == 45_000
    assert new_inv.replaces_invoice_id == data["inv_normal"].id


def test_locale_parity_for_void_keys():
    """English and Amharic locale files must maintain 100% key parity with Amharic reviewed tags."""
    import json
    from pathlib import Path

    locales_dir = Path("app/channels/telegram/locales")
    with open(locales_dir / "en.json", "r", encoding="utf-8") as f:
        en = json.load(f)
    with open(locales_dir / "am.json", "r", encoding="utf-8") as f:
        am = json.load(f)

    assert set(en.keys()) == set(am.keys())

    # Check that new void keys exist and am keys are marked
    void_keys = [
        "void_sale_action",
        "void_sale_confirm_prompt",
        "void_reason_wrong_amount",
        "void_reason_duplicate_entry",
        "void_reason_wrong_retailer",
        "void_reason_cancelled_sale",
        "void_reason_other",
        "void_success_msg",
        "void_blocked_confirmed_match",
        "void_and_reenter_action",
    ]
    for k in void_keys:
        assert k in en
        assert k in am
        assert "needs native review" in am[k]


def test_html_escaping_in_confirm_dialogs(client: TestClient, db_session: Session, stage1_setup):
    """Invoice numbers with special characters are safely escaped in HTML."""
    data = stage1_setup
    set_tenant(db_session, data["tenant1"].id)

    # Create invoice with characters that need escaping
    inv_special, _ = record_credit_sale(
        db_session,
        tenant_id=data["tenant1"].id,
        user_id=data["owner"].id,
        invoice_number="INV-&<special>",
        retailer_id=data["ret1"].id,
        amount_santim=10_000_00,
        issued_date=date.today(),
        due_date=date.today() + timedelta(days=30),
    )
    db_session.commit()

    authenticate_client(client, db_session, data["owner"])
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    html = resp.text

    # Verify raw unescaped script/tag is not present
    assert "<SPECIAL>" not in html
    # Verify escaped representation is present
    assert "INV-&amp;&lt;SPECIAL&gt;" in html
