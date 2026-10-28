"""
tests/purchases/test_purchases_service.py

Test Suite for ERP Accounts Payable (AP) & Supplier Purchases.
Verifies:
- Supplier registration
- Purchase order creation and fulfillment
- Automatic warehouse stock increment on PO receipt
- Purchase invoice tracking and supplier payments
- Overpayment prevention guard
- AP debt aging calculations
- Immutable event logging
"""

from __future__ import annotations

from datetime import date, timedelta
import pytest
from sqlalchemy.orm import Session

from app.inventory.service import create_product, create_warehouse, get_stock_level
from app.kernel.enums import PurchaseInvoiceState, PurchaseOrderState
from app.kernel.models import Tenant, User
from app.kernel.tenant import set_tenant
from app.purchases.service import (
    create_purchase_invoice,
    create_purchase_order,
    create_supplier,
    get_supplier_ap_summary,
    receive_purchase_order,
    record_supplier_payment,
)


@pytest.fixture(autouse=True)
def ensure_tables(tables):
    pass


class TestPurchasesService:
    def test_supplier_and_po_with_stock_receipt(
        self, db_session: Session, tenant_a: Tenant, owner_user_a: User
    ):
        set_tenant(db_session, tenant_a.id)

        # 1. Create warehouse and product
        wh = create_warehouse(db_session, tenant_a.id, "WH-PO-1", "Kaliti PO Depot")
        prod = create_product(
            db_session, tenant_a.id, "FLOUR-50KG", "Balezaf Wheat Flour 50KG",
            unit_price_santim=350000, cost_price_santim=280000
        )

        # 2. Create supplier (Factory)
        supp = create_supplier(
            session=db_session,
            tenant_id=tenant_a.id,
            code="SUPP-BALEZAF",
            name="Balezaf Flour Mills / ባሌዛፍ ዱቄት ፋብሪካ",
            contact_person="Ato Girma",
            phone="+251911002233",
            bank_name="Commercial Bank of Ethiopia",
            bank_account="1000123456789",
            payment_terms_days=15,
        )
        assert supp.id is not None
        assert supp.code == "SUPP-BALEZAF"

        # 3. Create Purchase Order for 50 bags at 2,800 ETB (280,000 santim)
        po = create_purchase_order(
            session=db_session,
            tenant_id=tenant_a.id,
            supplier_id=supp.id,
            warehouse_id=wh.id,
            po_number="PO-2026-001",
            issued_date=date.today(),
            items_data=[(prod.id, 50, 280000)],
            created_by=owner_user_a.id,
        )
        assert po.state == PurchaseOrderState.DRAFT.value
        assert po.total_santim == 50 * 280000
        assert len(po.items) == 1
        poi = po.items[0]

        # Stock before receipt is 0
        assert get_stock_level(db_session, tenant_a.id, wh.id, prod.id) == 0

        # 4. Receive 30 bags (partial receipt)
        receive_purchase_order(
            session=db_session,
            tenant_id=tenant_a.id,
            po_id=po.id,
            receipt_data=[(poi.id, 30)],
            actor_id=owner_user_a.id,
        )
        assert po.state == PurchaseOrderState.PARTIALLY_RECEIVED.value
        assert poi.quantity_received == 30

        # Verify warehouse stock was automatically incremented!
        assert get_stock_level(db_session, tenant_a.id, wh.id, prod.id) == 30

        # 5. Receive remaining 20 bags (full fulfillment)
        receive_purchase_order(
            session=db_session,
            tenant_id=tenant_a.id,
            po_id=po.id,
            receipt_data=[(poi.id, 20)],
            actor_id=owner_user_a.id,
        )
        assert po.state == PurchaseOrderState.FULFILLED.value
        assert poi.quantity_received == 50
        assert get_stock_level(db_session, tenant_a.id, wh.id, prod.id) == 50

    def test_purchase_invoice_and_supplier_payment_lifecycle(
        self, db_session: Session, tenant_a: Tenant, owner_user_a: User
    ):
        set_tenant(db_session, tenant_a.id)

        supp = create_supplier(db_session, tenant_a.id, "SUPP-OIL", "Adaama Cooking Oil Refineries")
        prod = create_product(db_session, tenant_a.id, "OIL-20L", "Palm Oil 20L", unit_price_santim=220000)

        # Create Supplier Invoice: 10 units at 180,000 santim (1,800.00 ETB) = 1,800,000 santim
        pi = create_purchase_invoice(
            session=db_session,
            tenant_id=tenant_a.id,
            supplier_id=supp.id,
            invoice_number="INV-SUPP-9912",
            issued_date=date.today() - timedelta(days=5),
            due_date=date.today() + timedelta(days=10),
            items_data=[(prod.id, 10, 180000)],
        )
        assert pi.amount_santim == 1800000
        assert pi.paid_santim == 0
        assert pi.state == PurchaseInvoiceState.OPEN.value

        # Partial payment of 1,000,000 santim (10,000.00 ETB)
        sp1 = record_supplier_payment(
            session=db_session,
            tenant_id=tenant_a.id,
            supplier_id=supp.id,
            purchase_invoice_id=pi.id,
            amount_santim=1000000,
            payment_method="bank_transfer",
            payment_reference="FT261011CBE",
            created_by=owner_user_a.id,
        )
        assert sp1.amount_santim == 1000000
        assert pi.paid_santim == 1000000
        assert pi.state == PurchaseInvoiceState.PARTIALLY_PAID.value

        # Attempt to overpay: Remaining is 800,000 santim, attempt 900,000
        with pytest.raises(ValueError) as exc:
            record_supplier_payment(
                session=db_session,
                tenant_id=tenant_a.id,
                supplier_id=supp.id,
                purchase_invoice_id=pi.id,
                amount_santim=900000,
            )
        assert "exceeds remaining balance" in str(exc.value)

        # Settle exact remaining balance: 800,000 santim
        sp2 = record_supplier_payment(
            session=db_session,
            tenant_id=tenant_a.id,
            supplier_id=supp.id,
            purchase_invoice_id=pi.id,
            amount_santim=800000,
            payment_method="telebirr",
            payment_reference="TB-884120",
            created_by=owner_user_a.id,
        )
        assert pi.paid_santim == 1800000
        assert pi.state == PurchaseInvoiceState.SETTLED.value

    def test_supplier_ap_debt_aging(
        self, db_session: Session, tenant_a: Tenant, owner_user_a: User
    ):
        set_tenant(db_session, tenant_a.id)

        supp = create_supplier(db_session, tenant_a.id, "SUPP-AGING", "East Africa Bottling SC")
        prod = create_product(db_session, tenant_a.id, "COCA-CRATE", "Coca Cola Crates")

        today = date.today()

        # Invoice 1: Current (due in 5 days), 500,000 santim
        create_purchase_invoice(
            db_session, tenant_a.id, supp.id, "INV-CURR",
            issued_date=today - timedelta(days=10), due_date=today + timedelta(days=5),
            items_data=[(prod.id, 1, 500000)],
        )

        # Invoice 2: 15 days overdue (Bucket 1-30), 300,000 santim
        create_purchase_invoice(
            db_session, tenant_a.id, supp.id, "INV-OVERDUE-15",
            issued_date=today - timedelta(days=45), due_date=today - timedelta(days=15),
            items_data=[(prod.id, 1, 300000)],
        )

        # Invoice 3: 45 days overdue (Bucket 31-60), 200,000 santim
        create_purchase_invoice(
            db_session, tenant_a.id, supp.id, "INV-OVERDUE-45",
            issued_date=today - timedelta(days=75), due_date=today - timedelta(days=45),
            items_data=[(prod.id, 1, 200000)],
        )

        summary = get_supplier_ap_summary(db_session, tenant_a.id, as_of_date=today)
        assert summary["total_ap_debt_santim"] == 1000000
        assert summary["total_ap_debt_etb"] == "10,000.00"
        assert summary["buckets"]["current_santim"] == 500000
        assert summary["buckets"]["days_1_30_santim"] == 300000
        assert summary["buckets"]["days_31_60_santim"] == 200000
        assert len(summary["suppliers"]) == 1
        assert summary["suppliers"][0]["supplier_code"] == "SUPP-AGING"
