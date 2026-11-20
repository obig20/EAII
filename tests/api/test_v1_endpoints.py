"""
tests/api/test_v1_endpoints.py

Integration tests for the /api/v1 JSON adapter.  Needs the PostgreSQL test
database (see tests/conftest.py / docker-compose.test.yml).

Focus: the non-negotiable invariants —
  Rule 1  integer santim in storage, ETB floats only in responses
  Rule 2  L4 is never verified and can never be approved
  Rule 3  tenant isolation, and no anonymous retailer-id lookups
  Rule 4  every state change appends an event
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.kernel.auth import create_user_session
from app.kernel.db import get_session
from app.kernel.enums import EvidenceLevel, InvoiceState, MatchState, PaymentState, UserRole
from app.kernel.models import Event, Invoice, PaymentLine, PaymentMatch, Retailer, Tenant, User
from app.kernel.retailer_confirmation import create_retailer_confirmation_token
from app.kernel.tenant import set_tenant
from app.main import app
from tests.conftest import authenticate_client


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture
def client(db_session: Session):
    def _override():
        yield db_session

    app.dependency_overrides[get_session] = _override
    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def tenant(db_session: Session) -> Tenant:
    t = Tenant(id=uuid.uuid4(), name="API Tenant", created_at=_now())
    db_session.add(t)
    db_session.flush()
    set_tenant(db_session, t.id)
    return t


@pytest.fixture
def owner(db_session: Session, tenant: Tenant) -> User:
    u = User(id=uuid.uuid4(), tenant_id=tenant.id, role=UserRole.OWNER.value, display_name="Owner", created_at=_now())
    db_session.add(u)
    db_session.flush()
    return u


@pytest.fixture
def cashier(db_session: Session, tenant: Tenant) -> User:
    u = User(id=uuid.uuid4(), tenant_id=tenant.id, role=UserRole.CASHIER.value, display_name="Cashier", created_at=_now())
    db_session.add(u)
    db_session.flush()
    return u


@pytest.fixture
def shop(db_session: Session, tenant: Tenant) -> Retailer:
    r = Retailer(id=uuid.uuid4(), tenant_id=tenant.id, name="Kebede Mini Market", phone="+251911234567", created_at=_now())
    db_session.add(r)
    db_session.flush()
    return r


def _invoice(db, tenant, user, shop, *, amount_santim=1_000_000, due_in_days=-10, number=None) -> Invoice:
    today = _now().date()
    inv = Invoice(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        invoice_number=number or f"INV-{uuid.uuid4().hex[:8].upper()}",
        retailer_id=shop.id,
        amount_santim=amount_santim,
        issued_date=today - timedelta(days=30),
        due_date=today + timedelta(days=due_in_days),
        state=InvoiceState.OPEN.value,
        created_at=_now(),
        created_by=user.id,
    )
    db.add(inv)
    db.flush()
    return inv


def _proposed(db, tenant, inv, *, level="L2", amount_santim=None, name="Kebede") -> tuple[PaymentLine, PaymentMatch]:
    amt = amount_santim or inv.amount_santim
    pl = PaymentLine(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        source_ref="TX-1",
        row_key=uuid.uuid4().hex,
        amount_santim=amt,
        value_date=_now().date(),
        counterparty_name=name,
        raw_reference="payment",
        evidence_level=level,
        state=PaymentState.PROPOSED.value,
        created_at=_now(),
    )
    db.add(pl)
    db.flush()
    pm = PaymentMatch(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        payment_line_id=pl.id,
        invoice_id=inv.id,
        matched_santim=amt,
        tier="tier_b",
        state=MatchState.PROPOSED.value,
        created_at=_now(),
    )
    db.add(pm)
    db.flush()
    return pl, pm


def _events(db, tenant, event_type, aggregate_id=None) -> list[Event]:
    set_tenant(db, tenant.id)
    stmt = select(Event).where(Event.tenant_id == tenant.id, Event.event_type == event_type)
    if aggregate_id is not None:
        stmt = stmt.where(Event.aggregate_id == aggregate_id)
    return list(db.execute(stmt).scalars())


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
class TestAuth:
    @pytest.mark.parametrize(
        "method,path",
        [
            ("get", "/api/v1/dashboard/today"),
            ("get", "/api/v1/shops/overdue"),
            ("get", "/api/v1/review-queue"),
            ("post", f"/api/v1/review-queue/{uuid.uuid4()}/approve"),
            ("post", f"/api/v1/review-queue/{uuid.uuid4()}/reject"),
        ],
    )
    def test_protected_routes_require_login(self, client, method, path):
        assert getattr(client, method)(path).status_code == 401

    def test_sales_requires_login(self, client):
        r = client.post("/api/v1/sales", json={"shopId": str(uuid.uuid4()), "amount": 10, "dueInDays": 7})
        assert r.status_code == 401

    def test_bearer_token_works_without_csrf(self, client, db_session, tenant, owner, shop):
        _, raw = create_user_session(db_session, user=owner)
        r = client.post(
            "/api/v1/sales",
            json={"shopId": str(shop.id), "amount": 100, "dueInDays": 7},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200

    def test_cookie_session_needs_csrf_for_writes(self, client, db_session, tenant, owner, shop):
        authenticate_client(client, db_session, owner)
        del client.headers["x-csrf-token"]
        r = client.post("/api/v1/sales", json={"shopId": str(shop.id), "amount": 100, "dueInDays": 7})
        assert r.status_code == 403

    def test_no_store_header(self, client, db_session, tenant, owner):
        authenticate_client(client, db_session, owner)
        assert client.get("/api/v1/dashboard/today").headers["cache-control"] == "no-store"


class TestCors:
    def test_allowed_origin(self, client):
        r = client.options(
            "/api/v1/sales",
            headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"},
        )
        assert r.status_code == 200
        assert r.headers["access-control-allow-origin"] == "http://localhost:5173"
        assert r.headers["access-control-allow-credentials"] == "true"

    def test_unknown_origin_is_not_reflected(self, client):
        r = client.options(
            "/api/v1/sales",
            headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
        )
        assert "access-control-allow-origin" not in r.headers


# ---------------------------------------------------------------------------
# Dashboard & shops
# ---------------------------------------------------------------------------
class TestDashboardAndShops:
    def test_dashboard_totals_in_etb(self, client, db_session, tenant, owner, shop):
        inv = _invoice(db_session, tenant, owner, shop, amount_santim=1_000_000, due_in_days=-10)  # 10,000.00 ETB, 10d overdue
        other = Retailer(id=uuid.uuid4(), tenant_id=tenant.id, name="Selam", created_at=_now())
        db_session.add(other)
        db_session.flush()
        _invoice(db_session, tenant, owner, other, amount_santim=500_000, due_in_days=-40)  # 5,000.00 ETB, 40d overdue
        authenticate_client(client, db_session, owner)

        body = client.get("/api/v1/dashboard/today").json()
        assert body["totalOwed"] == 15000.0
        assert body["overdueTotal"] == 15000.0
        assert body["paidToday"] == 0.0
        # ordered by max overdue days DESC
        assert [s["name"] for s in body["topOverdue"]] == ["Selam", "Kebede Mini Market"]
        assert body["topOverdue"][1]["overdueDays"] == 10
        assert body["topOverdue"][1]["owed"] == 10000.0
        assert body["topOverdue"][1]["id"] == str(shop.id)

    def test_paid_today_counts_only_confirmed_matches(self, client, db_session, tenant, owner, shop):
        inv = _invoice(db_session, tenant, owner, shop, amount_santim=1_000_000)
        _, pm = _proposed(db_session, tenant, inv, amount_santim=400_000)
        authenticate_client(client, db_session, owner)

        assert client.get("/api/v1/dashboard/today").json()["paidToday"] == 0.0  # proposed != paid

        pm.state = MatchState.CONFIRMED.value
        pm.confirmed_at = _now()
        db_session.flush()
        body = client.get("/api/v1/dashboard/today").json()
        assert body["paidToday"] == 4000.0
        assert body["totalOwed"] == 6000.0

    def test_shops_overdue_first_and_includes_all(self, client, db_session, tenant, owner, shop):
        _invoice(db_session, tenant, owner, shop, due_in_days=-5)
        clean = Retailer(id=uuid.uuid4(), tenant_id=tenant.id, name="Aaa No Debt", created_at=_now())
        db_session.add(clean)
        db_session.flush()
        authenticate_client(client, db_session, owner)

        rows = client.get("/api/v1/shops/overdue").json()
        assert [r["name"] for r in rows] == ["Kebede Mini Market", "Aaa No Debt"]
        assert rows[1]["owed"] == 0.0 and rows[1]["lastSale"] == "No sales yet"
        assert set(rows[0]) == {"id", "name", "phone", "telegram", "owed", "overdueDays", "lastSale"}

    def test_other_tenants_data_is_invisible(self, client, db_session, tenant, owner, shop):
        t2 = Tenant(id=uuid.uuid4(), name="Other", created_at=_now())
        db_session.add(t2)
        db_session.flush()
        set_tenant(db_session, t2.id)
        u2 = User(id=uuid.uuid4(), tenant_id=t2.id, role="owner", display_name="O2", created_at=_now())
        r2 = Retailer(id=uuid.uuid4(), tenant_id=t2.id, name="Other Shop", created_at=_now())
        db_session.add_all([u2, r2])
        db_session.flush()
        _invoice(db_session, t2, u2, r2)
        authenticate_client(client, db_session, owner)

        assert [r["name"] for r in client.get("/api/v1/shops/overdue").json()] == ["Kebede Mini Market"]
        assert client.get("/api/v1/dashboard/today").json()["totalOwed"] == 0.0


# ---------------------------------------------------------------------------
# Sales
# ---------------------------------------------------------------------------
class TestSales:
    def test_records_integer_santim_and_event(self, client, db_session, tenant, owner, shop):
        authenticate_client(client, db_session, owner)
        r = client.post("/api/v1/sales", json={"shopId": str(shop.id), "amount": 1264.5, "dueInDays": 14})
        assert r.status_code == 200 and r.json() == {"ok": True}

        set_tenant(db_session, tenant.id)
        inv = db_session.execute(select(Invoice).where(Invoice.retailer_id == shop.id)).scalar_one()
        assert inv.amount_santim == 126450 and isinstance(inv.amount_santim, int)
        assert inv.invoice_number.startswith("INV-")
        assert inv.due_date - inv.issued_date == timedelta(days=14)
        assert _events(db_session, tenant, "invoice.created", inv.id)

    def test_cashier_may_record_sales(self, client, db_session, tenant, cashier, shop):
        authenticate_client(client, db_session, cashier)
        assert client.post("/api/v1/sales", json={"shopId": str(shop.id), "amount": 50, "dueInDays": 0}).status_code == 200

    @pytest.mark.parametrize("amount", [0, -1, 10.005])
    def test_rejects_bad_amounts(self, client, db_session, tenant, owner, shop, amount):
        authenticate_client(client, db_session, owner)
        r = client.post("/api/v1/sales", json={"shopId": str(shop.id), "amount": amount, "dueInDays": 7})
        assert r.status_code == 422

    def test_unknown_or_foreign_shop_is_404(self, client, db_session, tenant, owner):
        authenticate_client(client, db_session, owner)
        r = client.post("/api/v1/sales", json={"shopId": str(uuid.uuid4()), "amount": 10, "dueInDays": 7})
        assert r.status_code == 404

    def test_malformed_shop_id_is_422(self, client, db_session, tenant, owner):
        authenticate_client(client, db_session, owner)
        assert client.post("/api/v1/sales", json={"shopId": "shop-1", "amount": 10, "dueInDays": 7}).status_code == 422


# ---------------------------------------------------------------------------
# Review queue
# ---------------------------------------------------------------------------
class TestReviewQueue:
    def test_listing_and_evidence_ladder(self, client, db_session, tenant, owner, shop):
        inv = _invoice(db_session, tenant, owner, shop)
        l2, pm2 = _proposed(db_session, tenant, inv, level="L2")
        l4 = PaymentLine(
            id=uuid.uuid4(), tenant_id=tenant.id, row_key=uuid.uuid4().hex, amount_santim=185_000,
            value_date=_now().date(), counterparty_name="Meklit Grocers", raw_reference="photo",
            evidence_level="L4", state=PaymentState.UNMATCHED.value, created_at=_now(),
        )
        db_session.add(l4)
        db_session.flush()
        authenticate_client(client, db_session, owner)

        items = {i["id"]: i for i in client.get("/api/v1/review-queue").json()}
        matched = items[str(pm2.id)]
        assert matched["shopName"] == "Kebede Mini Market"
        assert matched["verification"] == "verified"
        assert matched["amount"] == inv.amount_santim / 100.0

        unmatched = items[str(l4.id)]  # no match -> payment line id
        assert unmatched["shopName"] == "Meklit Grocers"
        assert unmatched["verification"] == "unverified"  # Rule 2
        assert unmatched["receiptPhotoOnFile"] is True
        assert unmatched["amount"] == 1850.0

    def test_l4_with_proposed_match_is_never_verified(self, client, db_session, tenant, owner, shop):
        inv = _invoice(db_session, tenant, owner, shop)
        _, pm = _proposed(db_session, tenant, inv, level="L4")
        authenticate_client(client, db_session, owner)
        item = client.get("/api/v1/review-queue").json()[0]
        assert item["id"] == str(pm.id)
        assert item["verification"] == "unverified" and item["receiptPhotoOnFile"] is True

    def test_confirmed_and_rejected_lines_are_not_listed(self, client, db_session, tenant, owner, shop):
        inv = _invoice(db_session, tenant, owner, shop)
        pl, _ = _proposed(db_session, tenant, inv)
        pl.state = PaymentState.REJECTED.value
        db_session.flush()
        authenticate_client(client, db_session, owner)
        assert client.get("/api/v1/review-queue").json() == []


class TestApprove:
    def test_l4_cannot_be_approved(self, client, db_session, tenant, owner, shop):
        inv = _invoice(db_session, tenant, owner, shop)
        pl, pm = _proposed(db_session, tenant, inv, level="L4")
        authenticate_client(client, db_session, owner)

        r = client.post(f"/api/v1/review-queue/{pm.id}/approve")
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "insufficient_evidence"

        db_session.refresh(pm); db_session.refresh(pl); db_session.refresh(inv)
        assert pm.state == MatchState.PROPOSED.value
        assert pl.state == PaymentState.PROPOSED.value
        assert inv.state == InvoiceState.OPEN.value
        assert not _events(db_session, tenant, "match.api_confirmed", pm.id)

    def test_l2_approval_settles_invoice_and_emits_event(self, client, db_session, tenant, owner, shop):
        inv = _invoice(db_session, tenant, owner, shop)
        pl, pm = _proposed(db_session, tenant, inv, level="L2")
        authenticate_client(client, db_session, owner)

        r = client.post(f"/api/v1/review-queue/{pm.id}/approve")
        assert r.status_code == 200 and r.json() == {"ok": True}

        db_session.refresh(pm); db_session.refresh(pl); db_session.refresh(inv)
        assert pm.state == MatchState.CONFIRMED.value and pm.confirmed_by == owner.id and pm.confirmed_at
        assert pl.state == PaymentState.CONFIRMED.value
        assert inv.state == InvoiceState.SETTLED.value
        events = _events(db_session, tenant, "match.api_confirmed", pm.id)
        assert len(events) == 1 and events[0].actor_id == owner.id and events[0].evidence_level == "L2"

        # idempotent retry
        assert client.post(f"/api/v1/review-queue/{pm.id}/approve").status_code == 200
        assert len(_events(db_session, tenant, "match.api_confirmed", pm.id)) == 1

    def test_partial_payment_marks_invoice_partially_paid(self, client, db_session, tenant, owner, shop):
        inv = _invoice(db_session, tenant, owner, shop, amount_santim=1_000_000)
        pl, pm = _proposed(db_session, tenant, inv, level="L2", amount_santim=400_000)
        authenticate_client(client, db_session, owner)

        # amounts differ from the invoice's open balance -> explicit opt-in required
        r = client.post(f"/api/v1/review-queue/{pm.id}/approve")
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "amounts_differ_confirmation_required"

        r = client.post(f"/api/v1/review-queue/{pm.id}/approve", json={"confirm_differing": True})
        assert r.status_code == 200
        db_session.refresh(inv)
        assert inv.state == InvoiceState.PARTIALLY_PAID.value

    def test_cashier_cannot_approve(self, client, db_session, tenant, owner, cashier, shop):
        inv = _invoice(db_session, tenant, owner, shop)
        _, pm = _proposed(db_session, tenant, inv, level="L2")
        authenticate_client(client, db_session, cashier)
        assert client.post(f"/api/v1/review-queue/{pm.id}/approve").status_code == 403

    def test_line_without_match_is_409_and_unknown_is_404(self, client, db_session, tenant, owner):
        pl = PaymentLine(
            id=uuid.uuid4(), tenant_id=tenant.id, row_key=uuid.uuid4().hex, amount_santim=1000,
            value_date=_now().date(), evidence_level="L2", state=PaymentState.UNMATCHED.value, created_at=_now(),
        )
        db_session.add(pl)
        db_session.flush()
        authenticate_client(client, db_session, owner)
        assert client.post(f"/api/v1/review-queue/{pl.id}/approve").status_code == 409
        assert client.post(f"/api/v1/review-queue/{uuid.uuid4()}/approve").status_code == 404
        assert client.post("/api/v1/review-queue/not-a-uuid/approve").status_code == 422

    def test_cross_tenant_approve_is_404(self, client, db_session, tenant, owner, shop):
        inv = _invoice(db_session, tenant, owner, shop)
        _, pm = _proposed(db_session, tenant, inv, level="L2")

        t2 = Tenant(id=uuid.uuid4(), name="Other", created_at=_now())
        db_session.add(t2)
        db_session.flush()
        set_tenant(db_session, t2.id)
        o2 = User(id=uuid.uuid4(), tenant_id=t2.id, role="owner", display_name="O2", created_at=_now())
        db_session.add(o2)
        db_session.flush()
        authenticate_client(client, db_session, o2)

        assert client.post(f"/api/v1/review-queue/{pm.id}/approve").status_code == 404
        assert client.post(f"/api/v1/review-queue/{pm.id}/reject").status_code == 404
        set_tenant(db_session, tenant.id)
        db_session.refresh(pm)
        assert pm.state == MatchState.PROPOSED.value


class TestReject:
    def test_reject_match_rejects_line_and_logs(self, client, db_session, tenant, owner, shop):
        inv = _invoice(db_session, tenant, owner, shop)
        pl, pm = _proposed(db_session, tenant, inv, level="L2")
        authenticate_client(client, db_session, owner)

        r = client.post(f"/api/v1/review-queue/{pm.id}/reject")
        assert r.status_code == 200 and r.json() == {"ok": True}
        db_session.refresh(pm); db_session.refresh(pl); db_session.refresh(inv)
        assert pm.state == MatchState.REJECTED.value
        assert pl.state == PaymentState.REJECTED.value
        assert inv.state == InvoiceState.OPEN.value
        events = _events(db_session, tenant, "match.api_rejected", pm.id)
        assert len(events) == 1 and events[0].payload["payment_line_rejected"] is True
        assert client.post(f"/api/v1/review-queue/{pm.id}/reject").status_code == 200  # idempotent
        assert len(_events(db_session, tenant, "match.api_rejected", pm.id)) == 1

    def test_reject_unmatched_line(self, client, db_session, tenant, owner):
        pl = PaymentLine(
            id=uuid.uuid4(), tenant_id=tenant.id, row_key=uuid.uuid4().hex, amount_santim=2500,
            value_date=_now().date(), evidence_level="L4", state=PaymentState.UNMATCHED.value, created_at=_now(),
        )
        db_session.add(pl)
        db_session.flush()
        authenticate_client(client, db_session, owner)
        assert client.post(f"/api/v1/review-queue/{pl.id}/reject").status_code == 200
        db_session.refresh(pl)
        assert pl.state == PaymentState.REJECTED.value
        assert _events(db_session, tenant, "payment_line.api_rejected", pl.id)

    def test_confirmed_match_cannot_be_rejected_here(self, client, db_session, tenant, owner, shop):
        inv = _invoice(db_session, tenant, owner, shop)
        _, pm = _proposed(db_session, tenant, inv, level="L2")
        authenticate_client(client, db_session, owner)
        assert client.post(f"/api/v1/review-queue/{pm.id}/approve").status_code == 200
        assert client.post(f"/api/v1/review-queue/{pm.id}/reject").status_code == 409


# ---------------------------------------------------------------------------
# Public balances
# ---------------------------------------------------------------------------
class TestPublicBalances:
    def _token(self, db, tenant, shop) -> str:
        _, raw = create_retailer_confirmation_token(db, tenant_id=tenant.id, retailer_id=shop.id)
        return raw

    def test_balance_by_token_is_anonymous_and_nets_confirmed_payments(self, client, db_session, tenant, owner, shop):
        inv = _invoice(db_session, tenant, owner, shop, amount_santim=1_000_000, due_in_days=-3)
        _, pm = _proposed(db_session, tenant, inv, amount_santim=250_000)
        pm.state = MatchState.CONFIRMED.value
        pm.confirmed_at = _now()
        db_session.flush()
        raw = self._token(db_session, tenant, shop)

        r = client.get(f"/api/v1/public/balances/{raw}")  # no auth
        assert r.status_code == 200
        body = r.json()
        assert body["shopId"] == str(shop.id) and body["shopName"] == "Kebede Mini Market"
        assert body["owed"] == 7500.0 and body["overdue"] == 7500.0
        assert set(body) == {"shopId", "shopName", "owed", "overdue", "updatedAt"}

    def test_unknown_token_is_404(self, client):
        assert client.get("/api/v1/public/balances/not-a-real-token").status_code == 404

    def test_retailer_uuid_requires_login(self, client, db_session, tenant, owner, shop):
        _invoice(db_session, tenant, owner, shop, amount_santim=100_000)
        assert client.get(f"/api/v1/public/balances/{shop.id}").status_code == 404  # anonymous: no cross-tenant lookup

        authenticate_client(client, db_session, owner)
        r = client.get(f"/api/v1/public/balances/{shop.id}")
        assert r.status_code == 200 and r.json()["owed"] == 1000.0

    def test_retailer_uuid_from_other_tenant_is_404(self, client, db_session, tenant, owner, shop):
        t2 = Tenant(id=uuid.uuid4(), name="Other", created_at=_now())
        db_session.add(t2)
        db_session.flush()
        set_tenant(db_session, t2.id)
        o2 = User(id=uuid.uuid4(), tenant_id=t2.id, role="owner", display_name="O2", created_at=_now())
        db_session.add(o2)
        db_session.flush()
        authenticate_client(client, db_session, o2)
        assert client.get(f"/api/v1/public/balances/{shop.id}").status_code == 404

    def test_confirm_is_single_use_creates_no_payment_and_logs(self, client, db_session, tenant, owner, shop):
        _invoice(db_session, tenant, owner, shop)
        raw = self._token(db_session, tenant, shop)

        r = client.post(f"/api/v1/public/balances/{raw}/confirm")
        assert r.status_code == 200 and r.json() == {"ok": True}
        assert len(_events(db_session, tenant, "retailer.balance_confirmed")) == 1

        set_tenant(db_session, tenant.id)
        assert db_session.execute(select(PaymentLine)).first() is None  # Rule 2: confirming a balance is not payment evidence
        assert client.post(f"/api/v1/public/balances/{raw}/confirm").status_code == 409
        assert client.get(f"/api/v1/public/balances/{raw}").status_code == 409

    def test_dispute_validates_and_marks_invoices(self, client, db_session, tenant, owner, shop):
        inv = _invoice(db_session, tenant, owner, shop)
        raw = self._token(db_session, tenant, shop)

        assert client.post(f"/api/v1/public/balances/{raw}/dispute", json={"reason_code": "NOPE", "explanation": "x"}).status_code == 422
        assert client.post(f"/api/v1/public/balances/{raw}/dispute", json={"reason_code": "OTHER", "explanation": "  "}).status_code in (422,)
        assert client.post(f"/api/v1/public/balances/{raw}/dispute", json={}).status_code == 422

        r = client.post(
            f"/api/v1/public/balances/{raw}/dispute",
            json={"reason_code": "wrong_amount", "explanation": "I only took 5 cartons."},
        )
        assert r.status_code == 200 and r.json() == {"ok": True}
        db_session.refresh(inv)
        assert inv.state == InvoiceState.DISPUTED.value
        assert _events(db_session, tenant, "invoice.disputed", inv.id)
        assert _events(db_session, tenant, "retailer.balance_disputed")

    def test_public_endpoints_are_rate_limited_by_ip(self, client):
        codes = [client.get("/api/v1/public/balances/bad-token").status_code for _ in range(17)]
        assert codes[0] == 404 and codes[-1] == 429
