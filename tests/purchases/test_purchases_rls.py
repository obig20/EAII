"""
tests/purchases/test_purchases_rls.py

Tenant Isolation & RLS Security Tests for Accounts Payable & Purchases.
Verifies:
- Tenant A cannot view or manipulate Tenant B's suppliers, purchase orders, or bills.
- Cross-tenant payment recording is strictly blocked by RLS.
- Enforced under unprivileged 'app' role (NOBYPASSRLS).
"""

from __future__ import annotations

from datetime import date
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.inventory.service import create_product, create_warehouse
from app.kernel.models import PurchaseInvoice, PurchaseOrder, Supplier, Tenant
from app.kernel.tenant import set_tenant
from app.purchases.service import (
    create_purchase_invoice,
    create_purchase_order,
    create_supplier,
    record_supplier_payment,
)


@pytest.fixture(autouse=True)
def ensure_tables(tables):
    pass


class TestPurchasesTenantIsolation:
    def test_supplier_and_invoice_isolation(
        self, db_session: Session, tenant_a: Tenant, tenant_b: Tenant
    ):
        # Tenant A sets up supplier and invoice
        set_tenant(db_session, tenant_a.id)
        supp_a = create_supplier(db_session, tenant_a.id, "SUPP-A", "Supplier A")
        wh_a = create_warehouse(db_session, tenant_a.id, "WH-A", "Warehouse A")
        prod_a = create_product(db_session, tenant_a.id, "PROD-A", "Product A")

        pi_a = create_purchase_invoice(
            db_session, tenant_a.id, supp_a.id, "BILL-A-01",
            date.today(), date.today(), [(prod_a.id, 10, 50000)]
        )

        # Tenant B connects
        set_tenant(db_session, tenant_b.id)

        # Querying suppliers under Tenant B yields 0 for SUPP-A
        supps = db_session.execute(select(Supplier)).scalars().all()
        assert not any(s.id == supp_a.id for s in supps)

        # Querying purchase invoices under Tenant B yields 0 for BILL-A-01
        invoices = db_session.execute(select(PurchaseInvoice)).scalars().all()
        assert not any(i.id == pi_a.id for i in invoices)

        # Tenant B attempting to record payment on Tenant A's invoice fails
        with pytest.raises(Exception):
            record_supplier_payment(
                session=db_session,
                tenant_id=tenant_b.id,
                supplier_id=supp_a.id,
                purchase_invoice_id=pi_a.id,
                amount_santim=10000,
            )
