"""
tests/inventory/test_inventory_service.py

Test Suite for ERP Inventory & Multi-Warehouse Management Service.
Verifies:
- Warehouse and Product creation
- Stock receipt and valuation tracking
- Stock dispatch and InsufficientStockError guard
- Multi-warehouse transfers
- Physical count adjustments and damage shrinkage
- Inventory valuation summary in Santim and ETB
- Immutable event ledger integration
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.inventory.service import (
    InsufficientStockError,
    adjust_stock,
    create_product,
    create_product_category,
    create_warehouse,
    dispatch_stock,
    get_stock_level,
    get_warehouse_stock_summary,
    receive_stock,
    transfer_stock,
)
from app.kernel.enums import StockMovementType
from app.kernel.models import Event, StockLevel, StockMovement, Tenant, User
from app.kernel.tenant import set_tenant


@pytest.fixture(autouse=True)
def ensure_tables(tables):
    """Ensure Alembic tables are created."""
    pass


class TestInventoryService:
    def test_warehouse_and_product_creation(self, db_session: Session, tenant_a: Tenant, owner_user_a: User):
        set_tenant(db_session, tenant_a.id)

        # 1. Create warehouse
        wh = create_warehouse(
            session=db_session,
            tenant_id=tenant_a.id,
            code="WH-KALITI",
            name="Kaliti Central Warehouse / ቃሊቲ ማዕከላዊ መጋዘን",
            location="Akaki-Kaliti Subcity, Addis Ababa",
            is_primary=True,
        )
        assert wh.id is not None
        assert wh.code == "WH-KALITI"
        assert wh.is_primary is True

        # 2. Create Category and Product
        cat = create_product_category(
            session=db_session,
            tenant_id=tenant_a.id,
            name="Edible Oil / ዘይት",
            code="OIL",
        )
        assert cat.id is not None

        prod = create_product(
            session=db_session,
            tenant_id=tenant_a.id,
            sku="OIL-SUN-5L",
            name="Sunflower Oil 5L / የሱፍ ዘይት 5 ሊትር",
            unit_of_measure="jerrycan",
            unit_price_santim=85000,   # 850.00 ETB
            cost_price_santim=72000,   # 720.00 ETB
            reorder_threshold=50,
            category_id=cat.id,
        )
        assert prod.id is not None
        assert prod.sku == "OIL-SUN-5L"
        assert prod.unit_price_santim == 85000

        # Verify initial stock level is 0
        qty = get_stock_level(db_session, tenant_a.id, wh.id, prod.id)
        assert qty == 0

    def test_receive_and_dispatch_stock(self, db_session: Session, tenant_a: Tenant, owner_user_a: User):
        set_tenant(db_session, tenant_a.id)

        wh = create_warehouse(db_session, tenant_a.id, "WH-MAIN", "Main Depot", is_primary=True)
        prod = create_product(
            db_session, tenant_a.id, "SUGAR-50KG", "Wonji Sugar 50KG",
            unit_of_measure="bag", unit_price_santim=450000, cost_price_santim=400000
        )

        # Receive 100 bags from Wonji factory
        mov_in = receive_stock(
            session=db_session,
            tenant_id=tenant_a.id,
            warehouse_id=wh.id,
            product_id=prod.id,
            quantity=100,
            unit_cost_santim=400000,
            reference="PO-WONJI-2026-01",
            actor_id=owner_user_a.id,
        )
        assert mov_in.quantity == 100
        assert mov_in.movement_type == StockMovementType.RECEIPT.value

        # On-hand must now be 100
        assert get_stock_level(db_session, tenant_a.id, wh.id, prod.id) == 100

        # Dispatch 30 bags for wholesale delivery
        mov_out = dispatch_stock(
            session=db_session,
            tenant_id=tenant_a.id,
            warehouse_id=wh.id,
            product_id=prod.id,
            quantity=30,
            reference="INV-2026-001",
            actor_id=owner_user_a.id,
        )
        assert mov_out.quantity == 30
        assert mov_out.movement_type == StockMovementType.DISPATCH_SALE.value

        # Remaining on-hand must be 70
        assert get_stock_level(db_session, tenant_a.id, wh.id, prod.id) == 70

        # Attempt to dispatch 80 bags (more than 70 available) must fail
        with pytest.raises(InsufficientStockError) as exc_info:
            dispatch_stock(
                session=db_session,
                tenant_id=tenant_a.id,
                warehouse_id=wh.id,
                product_id=prod.id,
                quantity=80,
                reference="INV-OVERDRAFT",
            )
        assert "Insufficient stock" in str(exc_info.value)

        # Stock remains at 70
        assert get_stock_level(db_session, tenant_a.id, wh.id, prod.id) == 70

    def test_multi_warehouse_transfer(self, db_session: Session, tenant_a: Tenant, owner_user_a: User):
        set_tenant(db_session, tenant_a.id)

        wh_central = create_warehouse(db_session, tenant_a.id, "WH-CENTRAL", "Central Warehouse")
        wh_merkato = create_warehouse(db_session, tenant_a.id, "WH-MERKATO", "Merkato Retail Depot")
        prod = create_product(db_session, tenant_a.id, "SOAP-B20", "B-29 Soap Carton", unit_price_santim=120000)

        # Receive 200 cartons at Central
        receive_stock(db_session, tenant_a.id, wh_central.id, prod.id, 200, unit_cost_santim=95000)
        assert get_stock_level(db_session, tenant_a.id, wh_central.id, prod.id) == 200
        assert get_stock_level(db_session, tenant_a.id, wh_merkato.id, prod.id) == 0

        # Transfer 50 cartons from Central to Merkato
        mov_tr = transfer_stock(
            session=db_session,
            tenant_id=tenant_a.id,
            source_warehouse_id=wh_central.id,
            destination_warehouse_id=wh_merkato.id,
            product_id=prod.id,
            quantity=50,
            reference="TR-001",
            notes="Restocking Merkato store",
            actor_id=owner_user_a.id,
        )
        assert mov_tr.movement_type == StockMovementType.TRANSFER_OUT.value
        assert mov_tr.quantity == 50

        # Central should have 150; Merkato should have 50
        assert get_stock_level(db_session, tenant_a.id, wh_central.id, prod.id) == 150
        assert get_stock_level(db_session, tenant_a.id, wh_merkato.id, prod.id) == 50

    def test_stock_adjustment_and_damage_shrinkage(self, db_session: Session, tenant_a: Tenant, owner_user_a: User):
        set_tenant(db_session, tenant_a.id)

        wh = create_warehouse(db_session, tenant_a.id, "WH-ADJ", "Adjustment WH")
        prod = create_product(db_session, tenant_a.id, "PASTA-500G", "Dire Dawa Pasta Carton", unit_price_santim=50000)

        receive_stock(db_session, tenant_a.id, wh.id, prod.id, 100, unit_cost_santim=40000)

        # Physical stocktake reveals 96 cartons (4 damaged/broken in transit)
        adjust_stock(
            session=db_session,
            tenant_id=tenant_a.id,
            warehouse_id=wh.id,
            product_id=prod.id,
            new_quantity=96,
            reason_notes="4 damaged packs water damage",
            actor_id=owner_user_a.id,
        )
        assert get_stock_level(db_session, tenant_a.id, wh.id, prod.id) == 96

    def test_warehouse_stock_summary_and_valuation(self, db_session: Session, tenant_a: Tenant, owner_user_a: User):
        set_tenant(db_session, tenant_a.id)

        wh = create_warehouse(db_session, tenant_a.id, "WH-VAL", "Valuation Depot")
        p1 = create_product(
            db_session, tenant_a.id, "P1", "Product 1",
            unit_price_santim=10000, cost_price_santim=8000, reorder_threshold=20
        )
        p2 = create_product(
            db_session, tenant_a.id, "P2", "Product 2",
            unit_price_santim=20000, cost_price_santim=15000, reorder_threshold=50
        )

        receive_stock(db_session, tenant_a.id, wh.id, p1.id, 100, unit_cost_santim=8000)
        receive_stock(db_session, tenant_a.id, wh.id, p2.id, 10, unit_cost_santim=15000) # Below threshold (10 <= 50)

        summary = get_warehouse_stock_summary(db_session, tenant_a.id, wh.id)
        assert summary["total_sku_count"] == 2
        assert summary["total_units_on_hand"] == 110

        # Cost valuation: 100 * 8000 + 10 * 15000 = 800,000 + 150,000 = 950,000 santim (9,500.00 ETB)
        assert summary["total_cost_valuation_santim"] == 950000
        assert summary["total_cost_valuation_etb"] == "9,500.00"

        # Low stock count: P2 should be flagged
        assert summary["low_stock_count"] == 1
        assert summary["low_stock_items"][0]["sku"] == "P2"

    def test_immutable_event_logging_for_stock(self, db_session: Session, tenant_a: Tenant, owner_user_a: User):
        set_tenant(db_session, tenant_a.id)

        wh = create_warehouse(db_session, tenant_a.id, "WH-EVT", "Event WH")
        prod = create_product(db_session, tenant_a.id, "EVT-ITEM", "Event Item")
        receive_stock(db_session, tenant_a.id, wh.id, prod.id, 50, reference="REF-EVT-1")

        # Query events table directly
        events = db_session.execute(
            select(Event).where(
                Event.tenant_id == tenant_a.id,
                Event.aggregate_type.in_(["warehouse", "product", "stock_movement"]),
            )
        ).scalars().all()

        types = [e.event_type for e in events]
        assert "warehouse.created" in types
        assert "product.created" in types
        assert "stock.received" in types
