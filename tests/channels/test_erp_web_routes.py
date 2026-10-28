"""
tests/channels/test_erp_web_routes.py

Web route and HTML rendering tests for Daily Close ERP modules:
- Inventory dashboard (/inventory)
- Purchases / AP dashboard (/purchases)
- Delivery fleet & cash-in-transit dashboard (/delivery)
- Accounting General Ledger dashboard (/accounting)
- Peachtree / Sage 50 CSV export (/accounting/exports/peachtree.csv)
- Ministry of Revenues (MOR) VAT declaration export (/accounting/exports/mor-vat.csv)
- Role authorization (Owner vs Cashier) and tenant isolation
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.accounting.service import init_default_chart_of_accounts, post_journal_entry
from app.channels.web.routes import get_session
from app.delivery.service import create_delivery_driver, create_delivery_trip
from app.inventory.service import create_product, create_warehouse, receive_stock
from app.kernel.enums import UserRole
from app.kernel.models import Tenant, User
from app.kernel.tenant import set_tenant
from app.main import app
from app.purchases.service import create_supplier
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
def erp_tenant(db_session: Session) -> Tenant:
    t = Tenant(id=uuid.uuid4(), name="Merkato ERP Wholesale", created_at=datetime.now(timezone.utc))
    db_session.add(t)
    db_session.flush()
    set_tenant(db_session, t.id)
    return t


@pytest.fixture
def erp_owner(db_session: Session, erp_tenant: Tenant) -> User:
    u = User(
        id=uuid.uuid4(),
        tenant_id=erp_tenant.id,
        role=UserRole.OWNER.value,
        display_name="Ato ERP Owner",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(u)
    db_session.flush()
    return u


@pytest.fixture
def erp_cashier(db_session: Session, erp_tenant: Tenant) -> User:
    u = User(
        id=uuid.uuid4(),
        tenant_id=erp_tenant.id,
        role=UserRole.CASHIER.value,
        display_name="Cashier Abebe",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(u)
    db_session.flush()
    return u


class TestERPWebRoutes:
    def test_unauthenticated_access_redirects_to_login(self, client: TestClient):
        for path in ["/inventory", "/purchases", "/delivery", "/accounting"]:
            resp = client.get(path, follow_redirects=False)
            assert resp.status_code in (302, 303, 307, 401)

    def test_inventory_dashboard_renders(
        self, client: TestClient, db_session: Session, erp_tenant: Tenant, erp_owner: User
    ):
        authenticate_client(client, db_session, erp_owner)
        set_tenant(db_session, erp_tenant.id)

        wh = create_warehouse(db_session, erp_tenant.id, "W-MERKATO", "Merkato Central")
        prod = create_product(db_session, erp_tenant.id, "SKU-OIL-01", "Sunflower Cooking Oil 5L", unit_price_santim=120000)
        receive_stock(
            session=db_session,
            tenant_id=erp_tenant.id,
            warehouse_id=wh.id,
            product_id=prod.id,
            quantity=50,
            unit_cost_santim=105000,
            reference="GRN-001",
        )
        db_session.commit()

        resp = client.get("/inventory")
        assert resp.status_code == 200
        assert "Warehouse &amp; Inventory Management" in resp.text or "Warehouse & Inventory Management" in resp.text
        assert "SKU-OIL-01" in resp.text
        assert "Sunflower Cooking Oil 5L" in resp.text
        assert "Merkato Central" in resp.text

    def test_purchases_dashboard_renders(
        self, client: TestClient, db_session: Session, erp_tenant: Tenant, erp_owner: User
    ):
        authenticate_client(client, db_session, erp_owner)
        set_tenant(db_session, erp_tenant.id)

        create_supplier(
            db_session,
            erp_tenant.id,
            "SUPP-WASH",
            "Mojo Edible Oil Factory",
            contact_person="Ato Hailu",
            phone="+251911445566",
            bank_name="CBE",
            bank_account="100029384756",
        )
        db_session.commit()

        resp = client.get("/purchases")
        assert resp.status_code == 200
        assert "Accounts Payable &amp; Supplier Purchases" in resp.text or "Accounts Payable & Supplier Purchases" in resp.text
        assert "SUPP-WASH" in resp.text
        assert "Mojo Edible Oil Factory" in resp.text
        assert "100029384756" in resp.text

    def test_delivery_dashboard_renders(
        self, client: TestClient, db_session: Session, erp_tenant: Tenant, erp_owner: User
    ):
        from app.kernel.models import Invoice, Retailer
        authenticate_client(client, db_session, erp_owner)
        set_tenant(db_session, erp_tenant.id)

        wh = create_warehouse(db_session, erp_tenant.id, "W-KALITI", "Kaliti Transit Hub")
        driver = create_delivery_driver(
            db_session, erp_tenant.id, "Kassahun Tadesse", "+251911778899", vehicle_plate="AA-3-B12345"
        )
        ret = Retailer(
            id=uuid.uuid4(),
            tenant_id=erp_tenant.id,
            name="Kaliti Kiosk",
            phone="+251911122334",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(ret)
        db_session.flush()

        inv = Invoice(
            id=uuid.uuid4(),
            tenant_id=erp_tenant.id,
            retailer_id=ret.id,
            invoice_number="INV-DELIV-001",
            amount_santim=250000,
            issued_date=datetime.now(timezone.utc).date(),
            due_date=datetime.now(timezone.utc).date(),
            created_at=datetime.now(timezone.utc),
            created_by=erp_owner.id,
        )
        db_session.add(inv)
        db_session.flush()

        create_delivery_trip(
            db_session, erp_tenant.id, driver.id, wh.id, "TRIP-001", [inv.id], created_by=erp_owner.id
        )
        db_session.commit()

        resp = client.get("/delivery")
        assert resp.status_code == 200
        assert "Fleet Delivery &amp; Cash-in-Transit Custody" in resp.text or "Fleet Delivery & Cash-in-Transit Custody" in resp.text
        assert "Kassahun Tadesse" in resp.text
        assert "AA-3-B12345" in resp.text
        assert "Kaliti Transit Hub" in resp.text

    def test_accounting_dashboard_renders(
        self, client: TestClient, db_session: Session, erp_tenant: Tenant, erp_owner: User
    ):
        authenticate_client(client, db_session, erp_owner)
        set_tenant(db_session, erp_tenant.id)

        init_default_chart_of_accounts(db_session, erp_tenant.id)
        db_session.commit()

        resp = client.get("/accounting")
        assert resp.status_code == 200
        assert "Double-Entry General Ledger &amp; Tax Compliance" in resp.text or "Double-Entry General Ledger & Tax Compliance" in resp.text
        assert "Trial Balance" in resp.text
        assert "Income Statement (P&amp;L)" in resp.text or "Income Statement (P&L)" in resp.text
        assert "Balance Sheet Summary" in resp.text
        assert "Balanced (Dr = Cr)" in resp.text

    def test_exports_owner_only_and_cashier_forbidden(
        self, client: TestClient, db_session: Session, erp_tenant: Tenant, erp_owner: User, erp_cashier: User
    ):
        # 1. As cashier: forbidden
        authenticate_client(client, db_session, erp_cashier)
        resp_peachtree = client.get("/accounting/exports/peachtree.csv")
        assert resp_peachtree.status_code == 403

        resp_mor = client.get("/accounting/exports/mor-vat.csv")
        assert resp_mor.status_code == 403

        # 2. As owner: authorized
        authenticate_client(client, db_session, erp_owner)
        set_tenant(db_session, erp_tenant.id)
        init_default_chart_of_accounts(db_session, erp_tenant.id)
        db_session.commit()

        resp_peachtree_owner = client.get("/accounting/exports/peachtree.csv")
        assert resp_peachtree_owner.status_code == 200
        assert "text/csv" in resp_peachtree_owner.headers.get("content-type", "")
        assert "Date,Reference,Description,GL Account" in resp_peachtree_owner.text

        resp_mor_owner = client.get("/accounting/exports/mor-vat.csv")
        assert resp_mor_owner.status_code == 200
        assert "text/csv" in resp_mor_owner.headers.get("content-type", "")
        assert "MINISTRY OF REVENUES (MOR)" in resp_mor_owner.text
        assert "VAT 15%" in resp_mor_owner.text

    def test_erp_tenant_isolation_in_web(
        self, client: TestClient, db_session: Session, erp_tenant: Tenant, erp_owner: User
    ):
        # Create second tenant
        tenant_b = Tenant(id=uuid.uuid4(), name="Competitor Tenant", created_at=datetime.now(timezone.utc))
        db_session.add(tenant_b)
        db_session.flush()

        set_tenant(db_session, tenant_b.id)
        create_supplier(db_session, tenant_b.id, "SECRET-SUPP", "Competitor Secret Supplier")
        db_session.commit()

        # Log into tenant A and view /purchases
        authenticate_client(client, db_session, erp_owner)
        resp = client.get("/purchases")
        assert resp.status_code == 200
        assert "SECRET-SUPP" not in resp.text
        assert "Competitor Secret Supplier" not in resp.text
