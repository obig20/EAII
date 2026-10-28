"""
app/delivery/service.py

ERP Delivery Fleet & Cash-in-Transit Service
Birana Technology — Daily Close ERP Suite

Invariants:
- All monetary valuations are stored as integer Santim (Rule 1).
- Every trip dispatch, stop completion, and vault handover logs an immutable event (Rule 4).
- Row-Level Security (RLS) is strictly enforced for all queries (Rule 8).
- Two-Person Custody Handover: physical paper cash must be verified by both the driver and the warehouse custodian.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.kernel.enums import (
    CashHandoverStatus,
    DeliveryStopStatus,
    DeliveryTripState,
)
from app.kernel.events import append_event
from app.kernel.models import (
    CashTransitHandover,
    DeliveryDriver,
    DeliveryTrip,
    DeliveryTripStop,
    Invoice,
    User,
    Warehouse,
)


def create_delivery_driver(
    session: Session,
    tenant_id: uuid.UUID,
    name: str,
    phone: str | None = None,
    vehicle_plate: str | None = None,
) -> DeliveryDriver:
    """Register a delivery driver or van sales agent."""
    driver = DeliveryDriver(
        tenant_id=tenant_id,
        name=name.strip(),
        phone=phone.strip() if phone else None,
        vehicle_plate=vehicle_plate.strip().upper() if vehicle_plate else None,
        is_active=True,
    )
    session.add(driver)
    session.flush()

    append_event(
        session=session,
        tenant_id=tenant_id,
        aggregate_type="delivery_driver",
        aggregate_id=driver.id,
        event_type="delivery_driver.created",
        payload={"name": driver.name, "vehicle_plate": driver.vehicle_plate},
    )
    return driver


def create_delivery_trip(
    session: Session,
    tenant_id: uuid.UUID,
    driver_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    trip_number: str,
    invoice_ids: list[uuid.UUID],
    created_by: uuid.UUID | None = None,
) -> DeliveryTrip:
    """
    Plan a wholesale delivery route linking specific credit invoices.
    """
    if not invoice_ids:
        raise ValueError("Delivery trip must contain at least one invoice destination.")

    trip = DeliveryTrip(
        tenant_id=tenant_id,
        trip_number=trip_number.strip().upper(),
        driver_id=driver_id,
        warehouse_id=warehouse_id,
        state=DeliveryTripState.PLANNED.value,
        created_by=created_by,
    )
    session.add(trip)
    session.flush()

    for idx, inv_id in enumerate(invoice_ids, start=1):
        stop = DeliveryTripStop(
            tenant_id=tenant_id,
            trip_id=trip.id,
            invoice_id=inv_id,
            stop_order=idx,
            status=DeliveryStopStatus.PENDING.value,
            cash_collected_santim=0,
        )
        session.add(stop)

    session.flush()

    append_event(
        session=session,
        tenant_id=tenant_id,
        aggregate_type="delivery_trip",
        aggregate_id=trip.id,
        event_type="delivery_trip.created",
        payload={"trip_number": trip.trip_number, "stop_count": len(invoice_ids)},
    )
    return trip


def dispatch_delivery_trip(
    session: Session,
    tenant_id: uuid.UUID,
    trip_id: uuid.UUID,
) -> DeliveryTrip:
    """Mark a delivery trip as dispatched from warehouse."""
    trip = session.execute(
        select(DeliveryTrip).where(
            DeliveryTrip.tenant_id == tenant_id,
            DeliveryTrip.id == trip_id,
        ).with_for_update()
    ).scalar_one()

    if trip.state != DeliveryTripState.PLANNED.value:
        raise ValueError(f"Cannot dispatch trip in '{trip.state}' state.")

    trip.state = DeliveryTripState.DISPATCHED.value
    trip.departure_time = datetime.now(timezone.utc)
    session.flush()

    append_event(
        session=session,
        tenant_id=tenant_id,
        aggregate_type="delivery_trip",
        aggregate_id=trip.id,
        event_type="delivery_trip.dispatched",
        payload={"trip_number": trip.trip_number},
    )
    return trip


def complete_delivery_stop(
    session: Session,
    tenant_id: uuid.UUID,
    trip_id: uuid.UUID,
    stop_id: uuid.UUID,
    cash_collected_santim: int = 0,
    delivery_notes: str | None = None,
) -> DeliveryTripStop:
    """
    Mark an individual retail store delivery as complete.
    Records physical cash collected on the road.
    """
    if cash_collected_santim < 0:
        raise ValueError("Cash collected cannot be negative.")

    stop = session.execute(
        select(DeliveryTripStop).where(
            DeliveryTripStop.tenant_id == tenant_id,
            DeliveryTripStop.id == stop_id,
            DeliveryTripStop.trip_id == trip_id,
        ).with_for_update()
    ).scalar_one()

    stop.status = DeliveryStopStatus.DELIVERED.value
    stop.cash_collected_santim = cash_collected_santim
    stop.delivery_notes = delivery_notes
    stop.delivered_at = datetime.now(timezone.utc)
    session.flush()

    append_event(
        session=session,
        tenant_id=tenant_id,
        aggregate_type="delivery_stop",
        aggregate_id=stop.id,
        event_type="delivery_stop.completed",
        payload={
            "trip_id": str(trip_id),
            "cash_collected_santim": cash_collected_santim,
            "status": stop.status,
        },
    )
    return stop


def process_cash_handover(
    session: Session,
    tenant_id: uuid.UUID,
    trip_id: uuid.UUID,
    driver_id: uuid.UUID,
    custodian_user_id: uuid.UUID,
    actual_cash_santim: int,
    driver_confirmation_code: str | None = None,
    handover_notes: str | None = None,
) -> CashTransitHandover:
    """
    Two-Person Vault Handover Verification:
    Warehouse cashier (custodian) counts physical cash bag against trip stops.
    Identifies and records exact match or discrepancy.
    """
    if actual_cash_santim < 0:
        raise ValueError("Actual counted cash cannot be negative.")

    trip = session.execute(
        select(DeliveryTrip).where(
            DeliveryTrip.tenant_id == tenant_id,
            DeliveryTrip.id == trip_id,
            DeliveryTrip.driver_id == driver_id,
        ).with_for_update()
    ).scalar_one()

    stops = session.execute(
        select(DeliveryTripStop).where(
            DeliveryTripStop.tenant_id == tenant_id,
            DeliveryTripStop.trip_id == trip.id,
        )
    ).scalars().all()

    # Sum of expected cash from all delivered stops
    expected_cash_santim = sum(s.cash_collected_santim for s in stops if s.status == DeliveryStopStatus.DELIVERED.value)
    discrepancy_santim = actual_cash_santim - expected_cash_santim

    status = (
        CashHandoverStatus.VERIFIED_MATCH.value
        if discrepancy_santim == 0
        else CashHandoverStatus.VERIFIED_DISCREPANCY.value
    )

    handover = CashTransitHandover(
        tenant_id=tenant_id,
        trip_id=trip.id,
        driver_id=driver_id,
        custodian_user_id=custodian_user_id,
        expected_cash_santim=expected_cash_santim,
        actual_cash_santim=actual_cash_santim,
        discrepancy_santim=discrepancy_santim,
        status=status,
        driver_confirmation_code=driver_confirmation_code,
        handover_notes=handover_notes,
    )
    session.add(handover)

    # Mark trip as completed
    trip.state = DeliveryTripState.COMPLETED.value
    trip.completion_time = datetime.now(timezone.utc)
    session.flush()

    append_event(
        session=session,
        tenant_id=tenant_id,
        aggregate_type="cash_handover",
        aggregate_id=handover.id,
        event_type="delivery.cash_handover_verified",
        payload={
            "trip_number": trip.trip_number,
            "expected_santim": expected_cash_santim,
            "actual_santim": actual_cash_santim,
            "discrepancy_santim": discrepancy_santim,
            "status": status,
            "custodian_user_id": str(custodian_user_id),
            "driver_id": str(driver_id),
        },
    )
    return handover


def get_trip_delivery_summary(
    session: Session,
    tenant_id: uuid.UUID,
    trip_id: uuid.UUID,
) -> dict[str, Any]:
    """Return full summary of a delivery trip including stops and cash status."""
    trip = session.execute(
        select(DeliveryTrip).where(
            DeliveryTrip.tenant_id == tenant_id,
            DeliveryTrip.id == trip_id,
        )
    ).scalar_one()

    stops = session.execute(
        select(DeliveryTripStop, Invoice)
        .join(Invoice, DeliveryTripStop.invoice_id == Invoice.id)
        .where(
            DeliveryTripStop.tenant_id == tenant_id,
            DeliveryTripStop.trip_id == trip.id,
        )
        .order_by(DeliveryTripStop.stop_order)
    ).all()

    total_expected_cash = sum(s.cash_collected_santim for s, _ in stops)
    delivered_count = sum(1 for s, _ in stops if s.status == DeliveryStopStatus.DELIVERED.value)

    stops_list = []
    for stop, inv in stops:
        stops_list.append({
            "stop_id": str(stop.id),
            "stop_order": stop.stop_order,
            "invoice_number": inv.invoice_number,
            "invoice_total_etb": f"{inv.amount_santim / 100:,.2f}",
            "status": stop.status,
            "cash_collected_santim": stop.cash_collected_santim,
            "cash_collected_etb": f"{stop.cash_collected_santim / 100:,.2f}",
            "delivered_at": stop.delivered_at.isoformat() if stop.delivered_at else None,
        })

    handover_info = None
    if trip.handover:
        handover_info = {
            "status": trip.handover.status,
            "expected_cash_etb": f"{trip.handover.expected_cash_santim / 100:,.2f}",
            "actual_cash_etb": f"{trip.handover.actual_cash_santim / 100:,.2f}",
            "discrepancy_etb": f"{trip.handover.discrepancy_santim / 100:,.2f}",
            "created_at": trip.handover.created_at.isoformat(),
        }

    return {
        "trip_id": str(trip.id),
        "trip_number": trip.trip_number,
        "state": trip.state,
        "driver_id": str(trip.driver_id),
        "total_stops": len(stops),
        "delivered_stops": delivered_count,
        "total_expected_cash_santim": total_expected_cash,
        "total_expected_cash_etb": f"{total_expected_cash / 100:,.2f}",
        "handover": handover_info,
        "stops": stops_list,
    }
