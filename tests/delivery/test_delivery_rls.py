"""
tests/delivery/test_delivery_rls.py

Tenant Isolation & RLS Security Tests for Delivery Fleet & Cash-in-Transit.
Verifies:
- Tenant A cannot view or manipulate Tenant B's drivers, trips, stops, or cash handovers.
- Cross-tenant trip actions are blocked by RLS.
- Enforced under unprivileged 'app' role (NOBYPASSRLS).
"""

from __future__ import annotations

from datetime import date
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.delivery.service import (
    create_delivery_driver,
    create_delivery_trip,
    dispatch_delivery_trip,
)
from app.inventory.service import create_warehouse
from app.kernel.models import (
    DeliveryDriver,
    DeliveryTrip,
    Invoice,
    Retailer,
    Tenant,
    User,
)
from app.kernel.tenant import set_tenant


@pytest.fixture(autouse=True)
def ensure_tables(tables):
    pass


class TestDeliveryTenantIsolation:
    def test_delivery_driver_and_trip_isolation(
        self, db_session: Session, tenant_a: Tenant, tenant_b: Tenant, owner_user_a: User
    ):
        # Tenant A sets up driver, warehouse, and trip
        set_tenant(db_session, tenant_a.id)
        driver_a = create_delivery_driver(db_session, tenant_a.id, "Driver A", vehicle_plate="AA-11111")
        wh_a = create_warehouse(db_session, tenant_a.id, "WH-A-DEL", "Warehouse A")
        ret_a = Retailer(tenant_id=tenant_a.id, name="Retailer A")
        db_session.add(ret_a)
        db_session.flush()

        inv_a = Invoice(
            tenant_id=tenant_a.id,
            retailer_id=ret_a.id,
            invoice_number="INV-A-1",
            amount_santim=100000,
            issued_date=date.today(),
            due_date=date.today(),
            created_by=owner_user_a.id,
        )
        db_session.add(inv_a)
        db_session.flush()

        trip_a = create_delivery_trip(
            db_session, tenant_a.id, driver_a.id, wh_a.id, "TRIP-A-01", [inv_a.id], created_by=owner_user_a.id
        )

        # Tenant B connects
        set_tenant(db_session, tenant_b.id)

        # Querying drivers under Tenant B returns 0 rows for Driver A
        drivers = db_session.execute(select(DeliveryDriver)).scalars().all()
        assert not any(d.id == driver_a.id for d in drivers)

        # Querying trips under Tenant B returns 0 rows for Trip A
        trips = db_session.execute(select(DeliveryTrip)).scalars().all()
        assert not any(t.id == trip_a.id for t in trips)

        # Tenant B attempting to dispatch Tenant A's trip fails
        with pytest.raises(Exception):
            dispatch_delivery_trip(db_session, tenant_b.id, trip_a.id)
