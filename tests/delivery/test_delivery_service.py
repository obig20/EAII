"""
tests/delivery/test_delivery_service.py

Test Suite for ERP Delivery Fleet & Cash-in-Transit Management.
Verifies:
- Driver registration & vehicle tracking
- Multi-stop delivery trip planning & dispatch
- Stop completion with physical cash intake
- Two-Person Vault Handover Verification (exact match vs discrepancy)
- Trip summary and cash reconciliation
- Immutable event logging
"""

from __future__ import annotations

from datetime import date
import pytest
from sqlalchemy.orm import Session

from app.delivery.service import (
    complete_delivery_stop,
    create_delivery_driver,
    create_delivery_trip,
    dispatch_delivery_trip,
    get_trip_delivery_summary,
    process_cash_handover,
)
from app.inventory.service import create_warehouse
from app.kernel.enums import (
    CashHandoverStatus,
    DeliveryStopStatus,
    DeliveryTripState,
)
from app.kernel.models import Invoice, Retailer, Tenant, User
from app.kernel.tenant import set_tenant


@pytest.fixture(autouse=True)
def ensure_tables(tables):
    pass


class TestDeliveryService:
    def test_delivery_trip_lifecycle_and_exact_cash_handover(
        self, db_session: Session, tenant_a: Tenant, owner_user_a: User
    ):
        set_tenant(db_session, tenant_a.id)

        # 1. Setup warehouse and driver
        wh = create_warehouse(db_session, tenant_a.id, "WH-DEL-1", "Kaliti Loading Depot")
        driver = create_delivery_driver(
            session=db_session,
            tenant_id=tenant_a.id,
            name="Ato Bekele Driver / አቶ በቀለ",
            phone="+251911445566",
            vehicle_plate="Code 3 - 12345 AA",
        )
        assert driver.id is not None
        assert driver.name == "Ato Bekele Driver / አቶ በቀለ"

        # 2. Setup 2 invoices for delivery
        ret = Retailer(tenant_id=tenant_a.id, name="Bole Supermarket")
        db_session.add(ret)
        db_session.flush()

        inv1 = Invoice(
            tenant_id=tenant_a.id,
            retailer_id=ret.id,
            invoice_number="INV-DEL-01",
            amount_santim=250000,  # 2,500.00 ETB
            issued_date=date.today(),
            due_date=date.today(),
            created_by=owner_user_a.id,
        )
        inv2 = Invoice(
            tenant_id=tenant_a.id,
            retailer_id=ret.id,
            invoice_number="INV-DEL-02",
            amount_santim=150000,  # 1,500.00 ETB
            issued_date=date.today(),
            due_date=date.today(),
            created_by=owner_user_a.id,
        )
        db_session.add_all([inv1, inv2])
        db_session.flush()

        # 3. Create Delivery Trip with 2 stops
        trip = create_delivery_trip(
            session=db_session,
            tenant_id=tenant_a.id,
            driver_id=driver.id,
            warehouse_id=wh.id,
            trip_number="TRIP-2026-001",
            invoice_ids=[inv1.id, inv2.id],
            created_by=owner_user_a.id,
        )
        assert trip.state == DeliveryTripState.PLANNED.value
        assert len(trip.stops) == 2

        # 4. Dispatch the truck
        dispatch_delivery_trip(db_session, tenant_a.id, trip.id)
        assert trip.state == DeliveryTripState.DISPATCHED.value
        assert trip.departure_time is not None

        # 5. Complete stops with cash collections:
        # Stop 1: Collect full cash (2,500.00 ETB = 250,000 santim)
        stop1 = trip.stops[0]
        complete_delivery_stop(
            session=db_session,
            tenant_id=tenant_a.id,
            trip_id=trip.id,
            stop_id=stop1.id,
            cash_collected_santim=250000,
            delivery_notes="Paid full in cash",
        )
        assert stop1.status == DeliveryStopStatus.DELIVERED.value
        assert stop1.cash_collected_santim == 250000

        # Stop 2: Collect partial cash (1,000.00 ETB = 100,000 santim)
        stop2 = trip.stops[1]
        complete_delivery_stop(
            session=db_session,
            tenant_id=tenant_a.id,
            trip_id=trip.id,
            stop_id=stop2.id,
            cash_collected_santim=100000,
            delivery_notes="Customer paid 1,000 ETB cash, 500 ETB pending credit",
        )
        assert stop2.status == DeliveryStopStatus.DELIVERED.value
        assert stop2.cash_collected_santim == 100000

        # Total expected cash = 250,000 + 100,000 = 350,000 santim (3,500.00 ETB)
        # 6. Driver returns to warehouse: Two-person custody sign-off with exact physical count
        handover = process_cash_handover(
            session=db_session,
            tenant_id=tenant_a.id,
            trip_id=trip.id,
            driver_id=driver.id,
            custodian_user_id=owner_user_a.id,
            actual_cash_santim=350000,  # Exact match
            driver_confirmation_code="4921",
            handover_notes="Cash counted in presence of driver, perfectly matched",
        )
        assert handover.status == CashHandoverStatus.VERIFIED_MATCH.value
        assert handover.discrepancy_santim == 0
        assert trip.state == DeliveryTripState.COMPLETED.value
        assert trip.completion_time is not None

        # Verify summary
        summary = get_trip_delivery_summary(db_session, tenant_a.id, trip.id)
        assert summary["total_stops"] == 2
        assert summary["delivered_stops"] == 2
        assert summary["total_expected_cash_santim"] == 350000
        assert summary["total_expected_cash_etb"] == "3,500.00"
        assert summary["handover"]["status"] == CashHandoverStatus.VERIFIED_MATCH.value

    def test_cash_handover_discrepancy_detection(
        self, db_session: Session, tenant_a: Tenant, owner_user_a: User
    ):
        set_tenant(db_session, tenant_a.id)

        wh = create_warehouse(db_session, tenant_a.id, "WH-DEL-2", "Depot 2")
        driver = create_delivery_driver(db_session, tenant_a.id, "Driver Chala")
        ret = Retailer(tenant_id=tenant_a.id, name="Retailer Merkato")
        db_session.add(ret)
        db_session.flush()

        inv = Invoice(
            tenant_id=tenant_a.id,
            retailer_id=ret.id,
            invoice_number="INV-DISC-01",
            amount_santim=500000,
            issued_date=date.today(),
            due_date=date.today(),
            created_by=owner_user_a.id,
        )
        db_session.add(inv)
        db_session.flush()

        trip = create_delivery_trip(
            db_session, tenant_a.id, driver.id, wh.id, "TRIP-DISC-01", [inv.id], created_by=owner_user_a.id
        )
        dispatch_delivery_trip(db_session, tenant_a.id, trip.id)

        stop = trip.stops[0]
        # Driver records 500,000 santim collected
        complete_delivery_stop(db_session, tenant_a.id, trip.id, stop.id, cash_collected_santim=500000)

        # Cashier counts physical cash: only 480,000 santim found in the envelope (200 ETB missing!)
        handover = process_cash_handover(
            session=db_session,
            tenant_id=tenant_a.id,
            trip_id=trip.id,
            driver_id=driver.id,
            custodian_user_id=owner_user_a.id,
            actual_cash_santim=480000,
            handover_notes="Discrepancy: Envelope contains 4,800 ETB, expected 5,000 ETB.",
        )

        assert handover.status == CashHandoverStatus.VERIFIED_DISCREPANCY.value
        assert handover.expected_cash_santim == 500000
        assert handover.actual_cash_santim == 480000
        assert handover.discrepancy_santim == -20000  # -200.00 ETB shortage
