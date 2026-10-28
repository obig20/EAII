"""
app/purchases/service.py

ERP Accounts Payable (AP) & Supplier Purchases Service
Birana Technology — Daily Close ERP Suite

Invariants:
- All monetary valuations are stored as integer Santim (Rule 1).
- Every purchase order, invoice, and payment logs an immutable event (Rule 4).
- Row-Level Security (RLS) is strictly enforced for all queries (Rule 8).
- Supplier payments cannot exceed invoice outstanding amounts (check constraint + service lock).
- Receiving purchase order items automatically updates warehouse stock levels.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.inventory.service import receive_stock
from app.kernel.enums import PurchaseInvoiceState, PurchaseOrderState
from app.kernel.events import append_event
from app.kernel.models import (
    Product,
    PurchaseInvoice,
    PurchaseInvoiceItem,
    PurchaseOrder,
    PurchaseOrderItem,
    Supplier,
    SupplierPayment,
    Warehouse,
)


def create_supplier(
    session: Session,
    tenant_id: uuid.UUID,
    code: str,
    name: str,
    contact_person: str | None = None,
    phone: str | None = None,
    bank_name: str | None = None,
    bank_account: str | None = None,
    payment_terms_days: int = 30,
) -> Supplier:
    """Create a new factory / manufacturer / importer supplier."""
    supp = Supplier(
        tenant_id=tenant_id,
        code=code.strip().upper(),
        name=name.strip(),
        contact_person=contact_person.strip() if contact_person else None,
        phone=phone.strip() if phone else None,
        bank_name=bank_name.strip() if bank_name else None,
        bank_account=bank_account.strip() if bank_account else None,
        payment_terms_days=payment_terms_days,
    )
    session.add(supp)
    session.flush()

    append_event(
        session=session,
        tenant_id=tenant_id,
        aggregate_type="supplier",
        aggregate_id=supp.id,
        event_type="supplier.created",
        payload={"code": supp.code, "name": supp.name},
    )
    return supp


def create_purchase_order(
    session: Session,
    tenant_id: uuid.UUID,
    supplier_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    po_number: str,
    issued_date: date,
    items_data: list[tuple[uuid.UUID, int, int]],  # (product_id, quantity_ordered, unit_cost_santim)
    expected_delivery_date: date | None = None,
    notes: str | None = None,
    created_by: uuid.UUID | None = None,
) -> PurchaseOrder:
    """
    Create a new purchase order with line items.
    """
    if not items_data:
        raise ValueError("Purchase order must have at least one item.")

    total_santim = sum(qty * unit_cost for _, qty, unit_cost in items_data)
    if total_santim <= 0:
        raise ValueError("Purchase order total cost must be greater than zero.")

    po = PurchaseOrder(
        tenant_id=tenant_id,
        supplier_id=supplier_id,
        warehouse_id=warehouse_id,
        po_number=po_number.strip().upper(),
        state=PurchaseOrderState.DRAFT.value,
        total_santim=total_santim,
        issued_date=issued_date,
        expected_delivery_date=expected_delivery_date,
        notes=notes,
        created_by=created_by,
    )
    session.add(po)
    session.flush()

    for prod_id, qty, unit_cost in items_data:
        if qty <= 0 or unit_cost < 0:
            raise ValueError("Item quantity must be > 0 and unit cost >= 0")
        poi = PurchaseOrderItem(
            tenant_id=tenant_id,
            purchase_order_id=po.id,
            product_id=prod_id,
            quantity_ordered=qty,
            quantity_received=0,
            unit_cost_santim=unit_cost,
            total_cost_santim=qty * unit_cost,
        )
        session.add(poi)

    session.flush()

    append_event(
        session=session,
        tenant_id=tenant_id,
        aggregate_type="purchase_order",
        aggregate_id=po.id,
        event_type="purchase_order.created",
        payload={
            "po_number": po.po_number,
            "total_santim": po.total_santim,
            "item_count": len(items_data),
        },
    )
    return po


def receive_purchase_order(
    session: Session,
    tenant_id: uuid.UUID,
    po_id: uuid.UUID,
    receipt_data: list[tuple[uuid.UUID, int]],  # (purchase_order_item_id, quantity_received)
    actor_id: uuid.UUID | None = None,
) -> PurchaseOrder:
    """
    Receive items against an open purchase order.
    Directly increments warehouse stock levels via inventory service.
    """
    po = session.execute(
        select(PurchaseOrder).where(
            PurchaseOrder.tenant_id == tenant_id,
            PurchaseOrder.id == po_id,
        ).with_for_update()
    ).scalar_one()

    if po.state in (PurchaseOrderState.FULFILLED.value, PurchaseOrderState.CANCELLED.value):
        raise ValueError(f"Cannot receive items on a purchase order in '{po.state}' state.")

    for poi_id, qty_recv in receipt_data:
        if qty_recv <= 0:
            continue

        poi = session.execute(
            select(PurchaseOrderItem).where(
                PurchaseOrderItem.tenant_id == tenant_id,
                PurchaseOrderItem.id == poi_id,
                PurchaseOrderItem.purchase_order_id == po.id,
            ).with_for_update()
        ).scalar_one()

        if poi.quantity_received + qty_recv > poi.quantity_ordered:
            raise ValueError(
                f"Received quantity ({poi.quantity_received + qty_recv}) exceeds ordered quantity ({poi.quantity_ordered})"
            )

        poi.quantity_received += qty_recv

        # Automatically receive stock into the target warehouse!
        receive_stock(
            session=session,
            tenant_id=tenant_id,
            warehouse_id=po.warehouse_id,
            product_id=poi.product_id,
            quantity=qty_recv,
            unit_cost_santim=poi.unit_cost_santim,
            reference=f"PO:{po.po_number}",
            actor_id=actor_id,
        )

    # Check overall PO fulfillment state
    all_items = session.execute(
        select(PurchaseOrderItem).where(PurchaseOrderItem.purchase_order_id == po.id)
    ).scalars().all()

    all_fulfilled = all(item.quantity_received >= item.quantity_ordered for item in all_items)
    any_received = any(item.quantity_received > 0 for item in all_items)

    if all_fulfilled:
        po.state = PurchaseOrderState.FULFILLED.value
    elif any_received:
        po.state = PurchaseOrderState.PARTIALLY_RECEIVED.value

    session.flush()

    append_event(
        session=session,
        tenant_id=tenant_id,
        aggregate_type="purchase_order",
        aggregate_id=po.id,
        event_type="purchase_order.received",
        payload={"po_number": po.po_number, "new_state": po.state},
    )
    return po


def create_purchase_invoice(
    session: Session,
    tenant_id: uuid.UUID,
    supplier_id: uuid.UUID,
    invoice_number: str,
    issued_date: date,
    due_date: date,
    items_data: list[tuple[uuid.UUID, int, int]],  # (product_id, quantity, unit_cost_santim)
    purchase_order_id: uuid.UUID | None = None,
) -> PurchaseInvoice:
    """
    Create a supplier purchase bill / invoice.
    """
    if not items_data:
        raise ValueError("Purchase invoice must have at least one line item.")

    amount_santim = sum(qty * cost for _, qty, cost in items_data)
    if amount_santim <= 0:
        raise ValueError("Purchase invoice amount must be positive.")

    pi = PurchaseInvoice(
        tenant_id=tenant_id,
        supplier_id=supplier_id,
        purchase_order_id=purchase_order_id,
        invoice_number=invoice_number.strip().upper(),
        amount_santim=amount_santim,
        paid_santim=0,
        state=PurchaseInvoiceState.OPEN.value,
        issued_date=issued_date,
        due_date=due_date,
    )
    session.add(pi)
    session.flush()

    for prod_id, qty, unit_cost in items_data:
        pii = PurchaseInvoiceItem(
            tenant_id=tenant_id,
            purchase_invoice_id=pi.id,
            product_id=prod_id,
            quantity=qty,
            unit_cost_santim=unit_cost,
            total_cost_santim=qty * unit_cost,
        )
        session.add(pii)

    session.flush()

    append_event(
        session=session,
        tenant_id=tenant_id,
        aggregate_type="purchase_invoice",
        aggregate_id=pi.id,
        event_type="purchase_invoice.created",
        payload={
            "invoice_number": pi.invoice_number,
            "amount_santim": pi.amount_santim,
        },
    )
    return pi


def record_supplier_payment(
    session: Session,
    tenant_id: uuid.UUID,
    supplier_id: uuid.UUID,
    purchase_invoice_id: uuid.UUID,
    amount_santim: int,
    payment_method: str = "bank_transfer",
    payment_reference: str | None = None,
    payment_date: date | None = None,
    notes: str | None = None,
    created_by: uuid.UUID | None = None,
) -> SupplierPayment:
    """
    Record an outgoing settlement payment to a supplier.
    Enforces that payment cannot exceed invoice outstanding balance.
    """
    if amount_santim <= 0:
        raise ValueError("Supplier payment amount must be a positive integer santim.")

    pi = session.execute(
        select(PurchaseInvoice).where(
            PurchaseInvoice.tenant_id == tenant_id,
            PurchaseInvoice.id == purchase_invoice_id,
            PurchaseInvoice.supplier_id == supplier_id,
        ).with_for_update()
    ).scalar_one()

    remaining_santim = pi.amount_santim - pi.paid_santim
    if amount_santim > remaining_santim:
        raise ValueError(
            f"Payment amount ({amount_santim} santim) exceeds remaining balance "
            f"({remaining_santim} santim) on supplier invoice {pi.invoice_number}."
        )

    pi.paid_santim += amount_santim
    if pi.paid_santim >= pi.amount_santim:
        pi.state = PurchaseInvoiceState.SETTLED.value
    else:
        pi.state = PurchaseInvoiceState.PARTIALLY_PAID.value

    sp = SupplierPayment(
        tenant_id=tenant_id,
        supplier_id=supplier_id,
        purchase_invoice_id=pi.id,
        amount_santim=amount_santim,
        payment_method=payment_method,
        payment_reference=payment_reference,
        payment_date=payment_date or date.today(),
        notes=notes,
        created_by=created_by,
    )
    session.add(sp)
    session.flush()

    append_event(
        session=session,
        tenant_id=tenant_id,
        aggregate_type="supplier_payment",
        aggregate_id=sp.id,
        event_type="supplier_payment.recorded",
        payload={
            "invoice_number": pi.invoice_number,
            "amount_santim": amount_santim,
            "new_invoice_state": pi.state,
            "remaining_santim": pi.amount_santim - pi.paid_santim,
        },
    )
    return sp


def get_supplier_ap_summary(
    session: Session,
    tenant_id: uuid.UUID,
    as_of_date: date | None = None,
) -> dict[str, Any]:
    """
    Generate Accounts Payable (AP) debt aging summary:
    - Total supplier debt owed
    - Current vs overdue buckets (1-30, 31-60, 61-90, 90+ days)
    - Breakdown by supplier
    """
    ref_date = as_of_date or date.today()

    stmt = (
        select(PurchaseInvoice, Supplier)
        .join(Supplier, PurchaseInvoice.supplier_id == Supplier.id)
        .where(
            PurchaseInvoice.tenant_id == tenant_id,
            PurchaseInvoice.state.in_([
                PurchaseInvoiceState.OPEN.value,
                PurchaseInvoiceState.PARTIALLY_PAID.value,
            ]),
        )
    )

    rows = session.execute(stmt).all()

    total_debt_santim = 0
    bucket_current = 0
    bucket_1_30 = 0
    bucket_31_60 = 0
    bucket_61_90 = 0
    bucket_90_plus = 0

    supplier_debt: dict[str, dict[str, Any]] = {}

    for pi, supp in rows:
        unpaid = pi.amount_santim - pi.paid_santim
        total_debt_santim += unpaid

        days_past_due = (ref_date - pi.due_date).days
        if days_past_due <= 0:
            bucket_current += unpaid
        elif days_past_due <= 30:
            bucket_1_30 += unpaid
        elif days_past_due <= 60:
            bucket_31_60 += unpaid
        elif days_past_due <= 90:
            bucket_61_90 += unpaid
        else:
            bucket_90_plus += unpaid

        if supp.name not in supplier_debt:
            supplier_debt[supp.name] = {
                "supplier_code": supp.code,
                "supplier_name": supp.name,
                "unpaid_santim": 0,
                "invoice_count": 0,
            }
        supplier_debt[supp.name]["unpaid_santim"] += unpaid
        supplier_debt[supp.name]["invoice_count"] += 1

    suppliers_list = []
    for sname, sdata in supplier_debt.items():
        sdata["unpaid_etb"] = f"{sdata['unpaid_santim'] / 100:,.2f}"
        suppliers_list.append(sdata)

    return {
        "as_of_date": ref_date.isoformat(),
        "total_ap_debt_santim": total_debt_santim,
        "total_ap_debt_etb": f"{total_debt_santim / 100:,.2f}",
        "buckets": {
            "current_santim": bucket_current,
            "current_etb": f"{bucket_current / 100:,.2f}",
            "days_1_30_santim": bucket_1_30,
            "days_1_30_etb": f"{bucket_1_30 / 100:,.2f}",
            "days_31_60_santim": bucket_31_60,
            "days_31_60_etb": f"{bucket_31_60 / 100:,.2f}",
            "days_61_90_santim": bucket_61_90,
            "days_61_90_etb": f"{bucket_61_90 / 100:,.2f}",
            "days_90_plus_santim": bucket_90_plus,
            "days_90_plus_etb": f"{bucket_90_plus / 100:,.2f}",
        },
        "suppliers": suppliers_list,
    }
