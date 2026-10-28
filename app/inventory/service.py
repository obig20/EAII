"""
app/inventory/service.py

ERP Inventory & Stock Operations Service
Birana Technology — Daily Close ERP Suite

Invariants:
- All monetary valuations are stored as integer Santim (Rule 1).
- Every stock movement records an immutable event in the tenant ledger (Rule 4).
- Row-Level Security (RLS) is strictly enforced for all queries (Rule 8).
- Stock levels cannot become negative (enforced by DB check constraint and service guards).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.kernel.enums import StockMovementType
from app.kernel.events import append_event
from app.kernel.models import (
    Invoice,
    InvoiceItem,
    Product,
    ProductCategory,
    StockLevel,
    StockMovement,
    Warehouse,
)


class InsufficientStockError(ValueError):
    """Raised when an operation attempts to dispatch more stock than is on hand."""


def create_warehouse(
    session: Session,
    tenant_id: uuid.UUID,
    code: str,
    name: str,
    location: str | None = None,
    is_primary: bool = False,
) -> Warehouse:
    """Create a new warehouse location for a tenant."""
    clean_code = code.strip().upper()
    wh = Warehouse(
        tenant_id=tenant_id,
        code=clean_code,
        name=name.strip(),
        location=location.strip() if location else None,
        is_primary=is_primary,
    )
    session.add(wh)
    session.flush()

    append_event(
        session=session,
        tenant_id=tenant_id,
        aggregate_type="warehouse",
        aggregate_id=wh.id,
        event_type="warehouse.created",
        payload={"code": wh.code, "name": wh.name, "is_primary": is_primary},
    )
    return wh


def create_product_category(
    session: Session,
    tenant_id: uuid.UUID,
    name: str,
    code: str | None = None,
) -> ProductCategory:
    """Create a product category."""
    cat = ProductCategory(
        tenant_id=tenant_id,
        name=name.strip(),
        code=code.strip().upper() if code else None,
    )
    session.add(cat)
    session.flush()
    return cat


def create_product(
    session: Session,
    tenant_id: uuid.UUID,
    sku: str,
    name: str,
    unit_of_measure: str = "pcs",
    unit_price_santim: int = 0,
    cost_price_santim: int = 0,
    reorder_threshold: int = 10,
    category_id: uuid.UUID | None = None,
) -> Product:
    """Create a product catalog item."""
    if unit_price_santim < 0 or cost_price_santim < 0:
        raise ValueError("Product prices must be non-negative integer santim.")

    prod = Product(
        tenant_id=tenant_id,
        sku=sku.strip().upper(),
        name=name.strip(),
        unit_of_measure=unit_of_measure.strip().lower(),
        unit_price_santim=unit_price_santim,
        cost_price_santim=cost_price_santim,
        reorder_threshold=reorder_threshold,
        category_id=category_id,
    )
    session.add(prod)
    session.flush()

    append_event(
        session=session,
        tenant_id=tenant_id,
        aggregate_type="product",
        aggregate_id=prod.id,
        event_type="product.created",
        payload={
            "sku": prod.sku,
            "name": prod.name,
            "unit_price_santim": prod.unit_price_santim,
            "cost_price_santim": prod.cost_price_santim,
        },
    )
    return prod


def _get_or_create_stock_level(
    session: Session,
    tenant_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    product_id: uuid.UUID,
    for_update: bool = False,
) -> StockLevel:
    """Helper to retrieve or initialize a StockLevel record."""
    stmt = select(StockLevel).where(
        StockLevel.tenant_id == tenant_id,
        StockLevel.warehouse_id == warehouse_id,
        StockLevel.product_id == product_id,
    )
    if for_update:
        stmt = stmt.with_for_update()

    sl = session.execute(stmt).scalar_one_or_none()
    if sl is None:
        sl = StockLevel(
            tenant_id=tenant_id,
            warehouse_id=warehouse_id,
            product_id=product_id,
            quantity_on_hand=0,
            quantity_reserved=0,
        )
        session.add(sl)
        session.flush()
    return sl


def get_stock_level(
    session: Session,
    tenant_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    product_id: uuid.UUID,
) -> int:
    """Return the current on-hand quantity for a product in a warehouse."""
    sl = session.execute(
        select(StockLevel.quantity_on_hand).where(
            StockLevel.tenant_id == tenant_id,
            StockLevel.warehouse_id == warehouse_id,
            StockLevel.product_id == product_id,
        )
    ).scalar_one_or_none()
    return sl if sl is not None else 0


def receive_stock(
    session: Session,
    tenant_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    product_id: uuid.UUID,
    quantity: int,
    unit_cost_santim: int = 0,
    reference: str | None = None,
    notes: str | None = None,
    actor_id: uuid.UUID | None = None,
) -> StockMovement:
    """
    Receive incoming inventory from supplier or factory.
    Increases quantity_on_hand and logs immutable movement event.
    """
    if quantity <= 0:
        raise ValueError("Received quantity must be a positive integer.")
    if unit_cost_santim < 0:
        raise ValueError("Unit cost must be non-negative integer santim.")

    # Lock stock level row
    sl = _get_or_create_stock_level(session, tenant_id, warehouse_id, product_id, for_update=True)
    sl.quantity_on_hand += quantity
    sl.updated_at = datetime.now(timezone.utc)

    # Optional: Update product latest cost price
    prod = session.get(Product, product_id)
    if prod and unit_cost_santim > 0:
        prod.cost_price_santim = unit_cost_santim

    movement = StockMovement(
        tenant_id=tenant_id,
        movement_type=StockMovementType.RECEIPT.value,
        product_id=product_id,
        destination_warehouse_id=warehouse_id,
        quantity=quantity,
        unit_cost_santim=unit_cost_santim,
        reference=reference,
        notes=notes,
        actor_id=actor_id,
    )
    session.add(movement)
    session.flush()

    append_event(
        session=session,
        tenant_id=tenant_id,
        aggregate_type="stock_movement",
        aggregate_id=movement.id,
        event_type="stock.received",
        payload={
            "warehouse_id": str(warehouse_id),
            "product_id": str(product_id),
            "quantity": quantity,
            "unit_cost_santim": unit_cost_santim,
            "new_on_hand": sl.quantity_on_hand,
            "reference": reference,
        },
    )
    return movement


def dispatch_stock(
    session: Session,
    tenant_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    product_id: uuid.UUID,
    quantity: int,
    reference: str | None = None,
    notes: str | None = None,
    actor_id: uuid.UUID | None = None,
) -> StockMovement:
    """
    Dispatch inventory for an invoice sale or delivery.
    Raises InsufficientStockError if quantity on hand is insufficient.
    """
    if quantity <= 0:
        raise ValueError("Dispatch quantity must be a positive integer.")

    sl = _get_or_create_stock_level(session, tenant_id, warehouse_id, product_id, for_update=True)
    if sl.quantity_on_hand < quantity:
        raise InsufficientStockError(
            f"Insufficient stock for product {product_id} in warehouse {warehouse_id}. "
            f"Available: {sl.quantity_on_hand}, Requested: {quantity}"
        )

    sl.quantity_on_hand -= quantity
    sl.updated_at = datetime.now(timezone.utc)

    movement = StockMovement(
        tenant_id=tenant_id,
        movement_type=StockMovementType.DISPATCH_SALE.value,
        product_id=product_id,
        source_warehouse_id=warehouse_id,
        quantity=quantity,
        unit_cost_santim=0,
        reference=reference,
        notes=notes,
        actor_id=actor_id,
    )
    session.add(movement)
    session.flush()

    append_event(
        session=session,
        tenant_id=tenant_id,
        aggregate_type="stock_movement",
        aggregate_id=movement.id,
        event_type="stock.dispatched",
        payload={
            "warehouse_id": str(warehouse_id),
            "product_id": str(product_id),
            "quantity": quantity,
            "new_on_hand": sl.quantity_on_hand,
            "reference": reference,
        },
    )
    return movement


def transfer_stock(
    session: Session,
    tenant_id: uuid.UUID,
    source_warehouse_id: uuid.UUID,
    destination_warehouse_id: uuid.UUID,
    product_id: uuid.UUID,
    quantity: int,
    reference: str | None = None,
    notes: str | None = None,
    actor_id: uuid.UUID | None = None,
) -> StockMovement:
    """
    Transfer inventory between two warehouses belonging to the same tenant.
    """
    if source_warehouse_id == destination_warehouse_id:
        raise ValueError("Source and destination warehouses cannot be the same.")
    if quantity <= 0:
        raise ValueError("Transfer quantity must be a positive integer.")

    # Consistent lock ordering to prevent deadlock
    wh_first, wh_second = sorted([source_warehouse_id, destination_warehouse_id])
    sl_first = _get_or_create_stock_level(session, tenant_id, wh_first, product_id, for_update=True)
    sl_second = _get_or_create_stock_level(session, tenant_id, wh_second, product_id, for_update=True)

    source_sl = sl_first if source_warehouse_id == wh_first else sl_second
    dest_sl = sl_second if destination_warehouse_id == wh_second else sl_first

    if source_sl.quantity_on_hand < quantity:
        raise InsufficientStockError(
            f"Cannot transfer: insufficient stock in source warehouse. "
            f"Available: {source_sl.quantity_on_hand}, Requested: {quantity}"
        )

    source_sl.quantity_on_hand -= quantity
    dest_sl.quantity_on_hand += quantity

    now = datetime.now(timezone.utc)
    source_sl.updated_at = now
    dest_sl.updated_at = now

    movement = StockMovement(
        tenant_id=tenant_id,
        movement_type=StockMovementType.TRANSFER_OUT.value,
        product_id=product_id,
        source_warehouse_id=source_warehouse_id,
        destination_warehouse_id=destination_warehouse_id,
        quantity=quantity,
        reference=reference,
        notes=notes,
        actor_id=actor_id,
    )
    session.add(movement)
    session.flush()

    append_event(
        session=session,
        tenant_id=tenant_id,
        aggregate_type="stock_movement",
        aggregate_id=movement.id,
        event_type="stock.transferred",
        payload={
            "source_warehouse_id": str(source_warehouse_id),
            "destination_warehouse_id": str(destination_warehouse_id),
            "product_id": str(product_id),
            "quantity": quantity,
            "source_on_hand": source_sl.quantity_on_hand,
            "dest_on_hand": dest_sl.quantity_on_hand,
            "reference": reference,
        },
    )
    return movement


def adjust_stock(
    session: Session,
    tenant_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    product_id: uuid.UUID,
    new_quantity: int,
    reason_notes: str,
    actor_id: uuid.UUID | None = None,
) -> StockMovement:
    """
    Adjust physical stock following a physical stocktake or damage write-off.
    """
    if new_quantity < 0:
        raise ValueError("New stock quantity cannot be negative.")

    sl = _get_or_create_stock_level(session, tenant_id, warehouse_id, product_id, for_update=True)
    old_qty = sl.quantity_on_hand
    delta = new_quantity - old_qty
    if delta == 0:
        # No adjustment needed
        return None

    sl.quantity_on_hand = new_quantity
    sl.updated_at = datetime.now(timezone.utc)

    mov_type = (
        StockMovementType.ADJUSTMENT.value
        if delta > 0
        else StockMovementType.DAMAGE_SHRINKAGE.value
    )

    movement = StockMovement(
        tenant_id=tenant_id,
        movement_type=mov_type,
        product_id=product_id,
        source_warehouse_id=warehouse_id if delta < 0 else None,
        destination_warehouse_id=warehouse_id if delta > 0 else None,
        quantity=abs(delta),
        notes=f"Adjustment from {old_qty} to {new_quantity}. Reason: {reason_notes}",
        actor_id=actor_id,
    )
    session.add(movement)
    session.flush()

    append_event(
        session=session,
        tenant_id=tenant_id,
        aggregate_type="stock_movement",
        aggregate_id=movement.id,
        event_type="stock.adjusted",
        payload={
            "warehouse_id": str(warehouse_id),
            "product_id": str(product_id),
            "old_quantity": old_qty,
            "new_quantity": new_quantity,
            "delta": delta,
            "reason": reason_notes,
        },
    )
    return movement


def get_warehouse_stock_summary(
    session: Session,
    tenant_id: uuid.UUID,
    warehouse_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """
    Aggregate stock summary across warehouses:
    - total products tracked
    - total physical units on hand
    - total inventory valuation in Santim & ETB (based on cost_price_santim)
    - list of low stock items below reorder threshold
    """
    stmt = (
        select(
            Product.id,
            Product.sku,
            Product.name,
            Product.unit_of_measure,
            Product.unit_price_santim,
            Product.cost_price_santim,
            Product.reorder_threshold,
            Warehouse.name.label("warehouse_name"),
            StockLevel.quantity_on_hand,
        )
        .join(StockLevel, Product.id == StockLevel.product_id)
        .join(Warehouse, StockLevel.warehouse_id == Warehouse.id)
        .where(Product.tenant_id == tenant_id)
    )
    if warehouse_id:
        stmt = stmt.where(StockLevel.warehouse_id == warehouse_id)

    rows = session.execute(stmt).all()

    total_units = 0
    total_cost_valuation_santim = 0
    total_sales_valuation_santim = 0
    low_stock_items = []
    items = []

    for r in rows:
        qty = r.quantity_on_hand
        cost_val = qty * r.cost_price_santim
        sale_val = qty * r.unit_price_santim

        total_units += qty
        total_cost_valuation_santim += cost_val
        total_sales_valuation_santim += sale_val

        item_data = {
            "product_id": str(r.id),
            "sku": r.sku,
            "name": r.name,
            "unit": r.unit_of_measure,
            "warehouse": r.warehouse_name,
            "quantity_on_hand": qty,
            "reorder_threshold": r.reorder_threshold,
            "is_low_stock": qty <= r.reorder_threshold,
            "cost_value_etb": f"{cost_val / 100:,.2f}",
            "sale_value_etb": f"{sale_val / 100:,.2f}",
        }
        items.append(item_data)
        if item_data["is_low_stock"]:
            low_stock_items.append(item_data)

    return {
        "total_sku_count": len(items),
        "total_units_on_hand": total_units,
        "total_cost_valuation_santim": total_cost_valuation_santim,
        "total_cost_valuation_etb": f"{total_cost_valuation_santim / 100:,.2f}",
        "total_sales_valuation_santim": total_sales_valuation_santim,
        "total_sales_valuation_etb": f"{total_sales_valuation_santim / 100:,.2f}",
        "low_stock_count": len(low_stock_items),
        "low_stock_items": low_stock_items,
        "inventory": items,
    }


# Convenience alias for web channels
get_inventory_summary = get_warehouse_stock_summary

