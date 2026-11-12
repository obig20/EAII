"""
tests/reports/test_pilot_metrics.py

Comprehensive tests for Stage 5: Pilot Metrics & Baseline Audit Reports.
All data used in these tests is strictly SYNTHETIC and labeled sample data.
"""

from __future__ import annotations

import argparse
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.cli import cli_pilot_baseline, cli_pilot_metrics
from app.kernel.db import session_scope
from app.kernel.enums import InvoiceState, MatchState, MatchTier, UserRole
from app.kernel.events import append_event
from app.kernel.models import (
    Event,
    Invoice,
    PaymentLine,
    PaymentMatch,
    PilotSnapshot,
    Retailer,
    RetailerConfirmationToken,
    Tenant,
    User,
)
from app.kernel.retailer_confirmation import (
    confirm_retailer_balance,
    create_retailer_confirmation_token,
    dispute_retailer_balance,
)
from app.kernel.sales import record_credit_sale
from app.kernel.tenant import set_tenant
from app.reports.pilot_metrics import (
    capture_pilot_baseline,
    compute_pilot_metrics,
    generate_pilot_metrics_html,
)


def _dt(d: date, hour: int = 10) -> datetime:
    return datetime(d.year, d.month, d.day, hour, 0, 0, tzinfo=timezone.utc)


def test_pilot_baseline_capture_and_db(db_session: Session, tenant_a: Tenant, owner_user_a: User):
    """Test capturing baseline snapshot and DB storage under forced RLS."""
    set_tenant(db_session, tenant_a.id)
    today = date(2026, 11, 1)

    # Create synthetic retailer & invoices
    retailer = Retailer(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        name="Sample Retailer Baseline",
        phone="+251911000111",
        created_at=_dt(today),
    )
    db_session.add(retailer)
    db_session.flush()

    inv1 = Invoice(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        retailer_id=retailer.id,
        invoice_number="SAMPLE-BASE-001",
        amount_santim=500_000,  # 5,000 ETB
        issued_date=today - timedelta(days=30),
        due_date=today - timedelta(days=15),  # Overdue
        state=InvoiceState.OPEN.value,
        created_at=_dt(today - timedelta(days=30)),
        created_by=owner_user_a.id,
    )
    inv2 = Invoice(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        retailer_id=retailer.id,
        invoice_number="SAMPLE-BASE-002",
        amount_santim=300_000,  # 3,000 ETB
        issued_date=today,
        due_date=today + timedelta(days=15),  # Not overdue
        state=InvoiceState.OPEN.value,
        created_at=_dt(today),
        created_by=owner_user_a.id,
    )
    db_session.add_all([inv1, inv2])
    db_session.flush()

    # Capture baseline
    snapshot = capture_pilot_baseline(
        db_session,
        tenant_id=tenant_a.id,
        user_id=owner_user_a.id,
    )

    assert snapshot.id is not None
    assert snapshot.tenant_id == tenant_a.id
    assert snapshot.snapshot_type == "baseline"
    assert snapshot.invoice_count == 2
    assert snapshot.retailer_count >= 1
    assert snapshot.total_credit_sales_santim == 800_000
    assert snapshot.total_overdue_santim == 500_000

    # Verify event logged
    evt = db_session.execute(
        select(Event).where(
            Event.tenant_id == tenant_a.id,
            Event.event_type == "pilot.baseline_captured",
        )
    ).scalars().first()
    assert evt is not None
    assert evt.payload["total_credit_sales_santim"] == 800_000


def test_pilot_metrics_synthetic_indicators(
    db_session: Session,
    tenant_a: Tenant,
    owner_user_a: User,
):
    """Test full metrics computation on structured synthetic sample data."""
    set_tenant(db_session, tenant_a.id)

    from_date = date(2026, 10, 1)
    to_date = date(2026, 10, 31)

    retailer = Retailer(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        name="Sample Wholesale Store",
        phone="+251911222333",
        created_at=_dt(from_date),
    )
    db_session.add(retailer)
    db_session.flush()

    # 1. Log Velocity: 3 invoices
    # inv1: issued 2026-10-05, created same day (within 24h)
    inv1 = Invoice(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        retailer_id=retailer.id,
        invoice_number="SAMPLE-INV-101",
        amount_santim=100_000,
        issued_date=date(2026, 10, 5),
        due_date=date(2026, 10, 20),
        state=InvoiceState.SETTLED.value,
        created_at=datetime(2026, 10, 5, 14, 0, 0, tzinfo=timezone.utc),
        created_by=owner_user_a.id,
    )
    # inv2: issued 2026-10-06, created same day (within 24h)
    inv2 = Invoice(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        retailer_id=retailer.id,
        invoice_number="SAMPLE-INV-102",
        amount_santim=200_000,
        issued_date=date(2026, 10, 6),
        due_date=date(2026, 10, 21),
        state=InvoiceState.OPEN.value,
        created_at=datetime(2026, 10, 6, 16, 30, 0, tzinfo=timezone.utc),
        created_by=owner_user_a.id,
    )
    # inv3: issued 2026-10-10, created 5 days later (delayed, sequence gap SAMPLE-INV-103 missing -> 104)
    inv3 = Invoice(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        retailer_id=retailer.id,
        invoice_number="SAMPLE-INV-104",
        amount_santim=400_000,
        issued_date=date(2026, 10, 10),
        due_date=date(2026, 10, 25),
        state=InvoiceState.OPEN.value,
        created_at=datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc),
        created_by=owner_user_a.id,
    )
    db_session.add_all([inv1, inv2, inv3])
    db_session.flush()

    # 2. Collection Turnaround: Payment line and confirmed match for inv1
    # Issue date: 2026-10-05, Confirmed: 2026-10-10 -> 5 days turnaround
    pl1 = PaymentLine(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        source_ref="SAMPLE-PAY-01",
        amount_santim=100_000,
        value_date=date(2026, 10, 10),
        evidence_level="L1",
        state="confirmed",
        created_at=datetime(2026, 10, 10, 9, 0, 0, tzinfo=timezone.utc),
    )
    db_session.add(pl1)
    db_session.flush()

    pm1 = PaymentMatch(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        payment_line_id=pl1.id,
        invoice_id=inv1.id,
        matched_santim=100_000,
        tier=MatchTier.TIER_A.value,
        state=MatchState.CONFIRMED.value,
        confirmed_by=owner_user_a.id,
        confirmed_at=datetime(2026, 10, 10, 10, 0, 0, tzinfo=timezone.utc),
        created_at=datetime(2026, 10, 10, 9, 30, 0, tzinfo=timezone.utc),  # Decision took 30 mins
    )
    db_session.add(pm1)
    db_session.flush()

    # 3. Baseline snapshot for overdue comparison
    base_snapshot = PilotSnapshot(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        snapshot_date=date(2026, 10, 1),
        snapshot_type="baseline",
        total_credit_sales_santim=500_000,
        total_overdue_santim=500_000,
        invoice_count=2,
        retailer_count=1,
        created_by=owner_user_a.id,
    )
    db_session.add(base_snapshot)
    db_session.flush()

    # 4. Review Queue: 1 proposed match
    pl2 = PaymentLine(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        source_ref="SAMPLE-PAY-02",
        amount_santim=200_000,
        value_date=date(2026, 10, 20),
        evidence_level="L2",
        state="unmatched",
        created_at=datetime(2026, 10, 20, 11, 0, 0, tzinfo=timezone.utc),
    )
    db_session.add(pl2)
    db_session.flush()

    pm2 = PaymentMatch(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        payment_line_id=pl2.id,
        invoice_id=inv2.id,
        matched_santim=200_000,
        tier=MatchTier.TIER_B.value,
        state=MatchState.PROPOSED.value,
        created_at=datetime(2026, 10, 20, 11, 30, 0, tzinfo=timezone.utc),
    )
    db_session.add(pm2)
    db_session.flush()

    # 5. Events: Retailer link events
    evt_created = Event(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        event_type="retailer_token.created",
        aggregate_type="retailer_confirmation_token",
        aggregate_id=uuid.uuid4(),
        payload={},
        created_at=datetime(2026, 10, 12, 10, 0, 0, tzinfo=timezone.utc),
    )
    evt_viewed = Event(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        event_type="retailer_token.viewed",
        aggregate_type="retailer_confirmation_token",
        aggregate_id=uuid.uuid4(),
        payload={},
        created_at=datetime(2026, 10, 12, 11, 0, 0, tzinfo=timezone.utc),
    )
    evt_confirmed = Event(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        event_type="retailer.balance_confirmed",
        aggregate_type="retailer_confirmation_token",
        aggregate_id=uuid.uuid4(),
        payload={},
        created_at=datetime(2026, 10, 12, 11, 30, 0, tzinfo=timezone.utc),
    )
    db_session.add_all([evt_created, evt_viewed, evt_confirmed])
    db_session.flush()

    # Compute metrics
    result = compute_pilot_metrics(
        db_session,
        tenant_id=tenant_a.id,
        from_date=from_date,
        to_date=to_date,
    )
    m = result["metrics"]

    # Assertions
    # Log Velocity: 2 out of 3 within 24h = 66.7%
    assert m["log_velocity"]["sample_size"] == 3
    assert m["log_velocity"]["raw_pct"] == 66.7
    assert m["log_velocity"]["sequence_gaps"] >= 1  # SAMPLE-INV-103 detected as missing

    # Turnaround: 1 confirmed match, 5 days
    assert m["turnaround"]["sample_size"] == 1
    assert m["turnaround"]["raw_days"] == 5.0

    # Overdue vs Baseline: inv2 (200k) + inv3 (400k) = 600k overdue vs 500k baseline (+20.0%)
    assert m["overdue_trajectory"]["sample_size"] == 2
    assert m["overdue_trajectory"]["baseline_sample_size"] == 1
    assert m["overdue_trajectory"]["change_pct"] == "+20.0%"

    # Review queue: 1 proposed match, decision time 30 mins for pm1
    assert m["review_queue"]["current_queue_depth"] == 1
    assert m["review_queue"]["sample_size"] == 1
    assert "minutes" in m["review_queue"]["median_decision_time"]

    # Auto-confirm share: 2 matches in window (pm1 Tier A, pm2 Tier B) -> 50%
    assert m["auto_confirm_share"]["sample_size"] == 2
    assert m["auto_confirm_share"]["raw_pct"] == 50.0

    # Reversal rate: 0 reversals / 1 confirmed = 0.0%
    assert m["reversal_rate"]["sample_size"] == 1
    assert m["reversal_rate"]["value"] == "0.0%"

    # Retailer links: 1 generated, 1 viewed, 1 confirmed, 0 disputed
    assert m["retailer_links"]["sample_size"] == 1
    assert m["retailer_links"]["generated"] == 1
    assert m["retailer_links"]["viewed"] == 1
    assert m["retailer_links"]["confirmed"] == 1
    assert m["retailer_links"]["disputed"] == 0

    # Active days
    assert m["active_days"]["active_days_count"] >= 1
    assert m["active_days"]["sample_size"] == 31


def test_pilot_metrics_empty_tenant(db_session: Session, tenant_b: Tenant):
    """Test empty tenant displays 'not available' and does not throw exceptions."""
    set_tenant(db_session, tenant_b.id)

    from_date = date(2026, 10, 1)
    to_date = date(2026, 10, 15)

    result = compute_pilot_metrics(
        db_session,
        tenant_id=tenant_b.id,
        from_date=from_date,
        to_date=to_date,
    )
    m = result["metrics"]

    assert m["log_velocity"]["value"] == "not available"
    assert m["log_velocity"]["sample_size"] == 0
    assert m["log_velocity"]["sequence_gaps"] == "not available"

    assert m["turnaround"]["value"] == "not available"
    assert m["turnaround"]["sample_size"] == 0

    assert m["overdue_trajectory"]["value"] == "not available"
    assert m["overdue_trajectory"]["baseline_sample_size"] == 0

    assert m["review_queue"]["value"] == "not available"
    assert m["review_queue"]["sample_size"] == 0
    assert m["review_queue"]["current_queue_depth"] == 0

    assert m["auto_confirm_share"]["value"] == "not available"
    assert m["auto_confirm_share"]["sample_size"] == 0

    assert m["reversal_rate"]["value"] == "not available"
    assert m["reversal_rate"]["sample_size"] == 0

    assert m["retailer_links"]["value"] == "not available"
    assert m["retailer_links"]["sample_size"] == 0

    # HTML Generation works cleanly
    html = generate_pilot_metrics_html(result, tenant_name="Empty Tenant")
    assert "<!DOCTYPE html>" in html
    assert "not available" in html
    assert "N = 0" in html


def test_pilot_metrics_tiny_samples(db_session: Session, tenant_a: Tenant, owner_user_a: User):
    """Test tiny sample boundary: exactly 1 invoice, 1 payment line, 1 match."""
    set_tenant(db_session, tenant_a.id)

    from_date = date(2026, 11, 1)
    to_date = date(2026, 11, 5)

    ret = Retailer(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        name="Tiny Sample Retailer",
        phone="+251911888777",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(ret)
    db_session.flush()

    inv = Invoice(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        retailer_id=ret.id,
        invoice_number="TINY-001",
        amount_santim=50_000,
        issued_date=date(2026, 11, 2),
        due_date=date(2026, 11, 10),
        state=InvoiceState.OPEN.value,
        created_at=datetime(2026, 11, 2, 8, 0, 0, tzinfo=timezone.utc),
        created_by=owner_user_a.id,
    )
    db_session.add(inv)
    db_session.flush()

    pl = PaymentLine(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        source_ref="TINY-PAY-01",
        amount_santim=50_000,
        value_date=date(2026, 11, 3),
        evidence_level="L1",
        state="confirmed",
        created_at=datetime(2026, 11, 3, 9, 0, 0, tzinfo=timezone.utc),
    )
    db_session.add(pl)
    db_session.flush()

    pm = PaymentMatch(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        payment_line_id=pl.id,
        invoice_id=inv.id,
        matched_santim=50_000,
        tier=MatchTier.TIER_A.value,
        state=MatchState.CONFIRMED.value,
        confirmed_by=owner_user_a.id,
        confirmed_at=datetime(2026, 11, 3, 10, 0, 0, tzinfo=timezone.utc),
        created_at=datetime(2026, 11, 3, 9, 30, 0, tzinfo=timezone.utc),
    )
    db_session.add(pm)
    db_session.flush()

    result = compute_pilot_metrics(
        db_session,
        tenant_id=tenant_a.id,
        from_date=from_date,
        to_date=to_date,
    )
    m = result["metrics"]

    # 1 invoice logged within 24h -> 100.0%
    assert m["log_velocity"]["sample_size"] == 1
    assert m["log_velocity"]["raw_pct"] == 100.0

    # 1 confirmed match -> turnaround (2026-11-03 - 2026-11-02 = 1 day)
    assert m["turnaround"]["sample_size"] == 1
    assert m["turnaround"]["raw_days"] == 1.0

    # Auto-confirm share: 1 match Tier A -> 100.0%
    assert m["auto_confirm_share"]["sample_size"] == 1
    assert m["auto_confirm_share"]["raw_pct"] == 100.0

    # Reversal rate: 0 reversals / 1 confirmed -> 0.0%
    assert m["reversal_rate"]["sample_size"] == 1
    assert m["reversal_rate"]["value"] == "0.0%"


def test_pilot_metrics_tenant_isolation(
    db_session: Session,
    tenant_a: Tenant,
    tenant_b: Tenant,
    owner_user_a: User,
    owner_user_b: User,
):
    """Assert Tenant A metrics do not leak or incorporate Tenant B data."""
    set_tenant(db_session, tenant_a.id)

    # Invoices in Tenant A
    ret_a = Retailer(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        name="Retailer A",
        phone="+251911111111",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(ret_a)
    db_session.flush()

    inv_a = Invoice(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        retailer_id=ret_a.id,
        invoice_number="ISO-INV-A01",
        amount_santim=100_000,
        issued_date=date(2026, 11, 2),
        due_date=date(2026, 11, 20),
        state=InvoiceState.OPEN.value,
        created_at=_dt(date(2026, 11, 2)),
        created_by=owner_user_a.id,
    )
    db_session.add(inv_a)
    db_session.flush()

    # Invoices in Tenant B
    set_tenant(db_session, tenant_b.id)
    ret_b = Retailer(
        id=uuid.uuid4(),
        tenant_id=tenant_b.id,
        name="Retailer B",
        phone="+251922222222",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(ret_b)
    db_session.flush()

    inv_b1 = Invoice(
        id=uuid.uuid4(),
        tenant_id=tenant_b.id,
        retailer_id=ret_b.id,
        invoice_number="ISO-INV-B01",
        amount_santim=900_000,
        issued_date=date(2026, 11, 2),
        due_date=date(2026, 11, 20),
        state=InvoiceState.OPEN.value,
        created_at=_dt(date(2026, 11, 2)),
        created_by=owner_user_b.id,
    )
    db_session.add(inv_b1)
    db_session.flush()

    # Query Tenant A
    set_tenant(db_session, tenant_a.id)
    res_a = compute_pilot_metrics(
        db_session,
        tenant_id=tenant_a.id,
        from_date=date(2026, 11, 1),
        to_date=date(2026, 11, 10),
    )
    assert res_a["metrics"]["log_velocity"]["sample_size"] == 1
    # Tenant B's invoice 900,000 santim must NOT appear
    assert "SAMPLE-INV-B01" not in str(res_a)


def test_pilot_metrics_html_offline_properties():
    """Verify generated HTML is offline, self-contained, and contains zero remote dependencies."""
    synthetic_report = {
        "tenant_id": str(uuid.uuid4()),
        "from_date": "2026-10-01",
        "to_date": "2026-10-31",
        "metrics": {
            "log_velocity": {"value": "85.0%", "sample_size": 20, "sequence_gaps": 0},
            "turnaround": {"value": "4.2 days", "sample_size": 18},
            "overdue_trajectory": {
                "value": "-12.5%",
                "sample_size": 15,
                "baseline_sample_size": 1,
                "current_overdue_etb": "50,000.00 ETB",
                "baseline_overdue_etb": "57,142.85 ETB",
            },
            "review_queue": {"value": "1.2 hours", "current_queue_depth": 3, "sample_size": 15},
            "auto_confirm_share": {"value": "72.0%", "sample_size": 25, "auto_confirmed_count": 18},
            "reversal_rate": {"value": "0.0%", "sample_size": 18, "reversals_count": 0},
            "retailer_links": {
                "value": "12 confirmed / 15 generated",
                "sample_size": 15,
                "generated": 15,
                "viewed": 14,
                "confirmed": 12,
                "disputed": 1,
            },
            "active_days": {"value": "24 / 31 days (77.4%)", "sample_size": 31},
        },
    }

    doc = generate_pilot_metrics_html(synthetic_report, tenant_name="Synthetic Wholesaler PLC")

    assert "<!DOCTYPE html>" in doc
    # Strict offline guarantee: no remote scripts, stylesheets, or fonts
    assert "http://" not in doc
    assert "https://" not in doc
    assert "<script" not in doc
    assert "N = 20" in doc
    assert "N = 18" in doc
    assert "N = 31" in doc
    assert "Synthetic Wholesaler PLC" in doc


def test_retailer_link_viewed_event(client, db_session: Session, tenant_a: Tenant):
    """Assert visiting /confirm-balance/{raw_token} records 'retailer_token.viewed' without PII."""
    set_tenant(db_session, tenant_a.id)

    retailer = Retailer(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        name="View Test Retailer",
        phone="+251911999888",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(retailer)
    db_session.flush()

    token_rec, raw_token = create_retailer_confirmation_token(
        db_session,
        tenant_id=tenant_a.id,
        retailer_id=retailer.id,
        expires_in_days=7,
    )
    db_session.commit()

    # Retailer visits the link
    resp = client.get(f"/confirm-balance/{raw_token}")
    assert resp.status_code == 200

    # Query event
    view_evt = db_session.execute(
        select(Event).where(
            Event.tenant_id == tenant_a.id,
            Event.event_type == "retailer_token.viewed",
            Event.aggregate_id == token_rec.id,
        )
    ).scalars().first()

    assert view_evt is not None
    # Payload must have token_id only, zero personal data (no phone, no IP, no names)
    assert view_evt.payload == {"token_id": str(token_rec.id)}
    assert "phone" not in view_evt.payload
    assert "name" not in view_evt.payload


def test_pilot_cli_commands(tmp_path: Path, monkeypatch, db_session: Session, tenant_a: Tenant):
    """Test CLI commands pilot-baseline and pilot-metrics dispatch cleanly."""
    import contextlib

    @contextlib.contextmanager
    def mock_scope():
        yield db_session

    monkeypatch.setattr("app.cli.session_scope", mock_scope)

    out_file = tmp_path / "test_metrics.html"

    # 1. pilot-baseline CLI
    args_base = argparse.Namespace(
        tenant_id=str(tenant_a.id),
    )
    ret_base = cli_pilot_baseline(args_base)
    assert ret_base == 0

    # 2. pilot-metrics CLI
    args_pm = argparse.Namespace(
        tenant_id=str(tenant_a.id),
        from_date="2026-10-01",
        to_date="2026-11-01",
        out=str(out_file),
    )
    ret_pm = cli_pilot_metrics(args_pm)
    assert ret_pm == 0
    assert out_file.exists()
    assert out_file.stat().st_size > 500
