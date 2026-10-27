"""
tests/inventory/test_inventory_rls.py

Tenant Isolation & RLS Security Tests for ERP Inventory Module.
Verifies:
- Tenant A cannot read Tenant B's warehouses, products, or stock levels.
- Cross-tenant stock transfer or dispatch fails with RLS/not-found.
- All queries executed under the unprivileged 'app' role (NOBYPASSRLS).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.inventory.service import (
    InsufficientStockError,
    create_product,
    create_warehouse,
    dispatch_stock,
    get_stock_level,
    receive_stock,
    transfer_stock,
)
from app.kernel.models import Product, StockLevel, Tenant, Warehouse
from app.kernel.tenant import set_tenant


@pytest.fixture(autouse=True)
def ensure_tables(tables):
    pass


class TestInventoryTenantIsolation:
    def test_warehouse_and_product_tenant_isolation(
        self, db_session: Session, tenant_a: Tenant, tenant_b: Tenant
    ):
        # 1. Tenant A creates warehouse and product
        set_tenant(db_session, tenant_a.id)
        wh_a = create_warehouse(db_session, tenant_a.id, "WH-A", "Warehouse Tenant A")
        prod_a = create_product(db_session, tenant_a.id, "SKU-A", "Product A")
        receive_stock(db_session, tenant_a.id, wh_a.id, prod_a.id, 50)

        # 2. Tenant B connects
        set_tenant(db_session, tenant_b.id)

        # Querying warehouses under Tenant B context returns 0 rows for WH-A
        whs_b = db_session.execute(select(Warehouse)).scalars().all()
        assert not any(w.id == wh_a.id for w in whs_b)

        # Querying products under Tenant B context returns 0 rows for SKU-A
        prods_b = db_session.execute(select(Product)).scalars().all()
        assert not any(p.id == prod_a.id for p in prods_b)

        # Stock level query under Tenant B returns 0
        qty = get_stock_level(db_session, tenant_b.id, wh_a.id, prod_a.id)
        assert qty == 0

    def test_cross_tenant_stock_dispatch_blocked(
        self, db_session: Session, tenant_a: Tenant, tenant_b: Tenant
    ):
        # Tenant A has 100 units
        set_tenant(db_session, tenant_a.id)
        wh_a = create_warehouse(db_session, tenant_a.id, "WH-A-DEPOT", "Warehouse A")
        prod_a = create_product(db_session, tenant_a.id, "SKU-RICE", "Rice 25KG")
        receive_stock(db_session, tenant_a.id, wh_a.id, prod_a.id, 100)

        # Tenant B tries to dispatch Tenant A's stock
        set_tenant(db_session, tenant_b.id)
        with pytest.raises(InsufficientStockError):
            dispatch_stock(
                session=db_session,
                tenant_id=tenant_b.id,
                warehouse_id=wh_a.id,
                product_id=prod_a.id,
                quantity=10,
            )

        # Verify Tenant A's stock was untouched (still 100)
        set_tenant(db_session, tenant_a.id)
        assert get_stock_level(db_session, tenant_a.id, wh_a.id, prod_a.id) == 100
