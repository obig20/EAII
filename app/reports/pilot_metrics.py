"""
app/reports/pilot_metrics.py

Domain engine for Pilot Metrics and Baseline Reporting.
Generates cold-data, privacy-safe operational audit reports for pilot evaluation.

Key Rules & Invariants:
- All monetary arithmetic uses integer santim.
- Strict Row-Level Security: app.current_tenant session variable set for every query.
- Aggregates ONLY: zero personal data (no customer names, phone numbers, or operator PII).
- Every metric reports explicit sample size (N = ...).
- If data is missing or N == 0, outputs "not available" without throwing exceptions.
- Standalone self-contained HTML: inline CSS and SVG, zero external CDN links.
"""

from __future__ import annotations

import html
import math
import statistics
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.kernel.enums import InvoiceState, MatchState, MatchTier
from app.kernel.events import append_event
from app.kernel.models import (
    Event,
    Invoice,
    PayerAlias,
    PaymentLine,
    PaymentMatch,
    PilotSnapshot,
    Retailer,
    RetailerConfirmationToken,
    Tenant,
)
from app.kernel.money import format_money, to_etb
from app.kernel.sales import detect_sequence_gaps
from app.kernel.tenant import set_tenant
from app.matching.normalization import (
    normalize_account_number,
    normalize_ethiopian_name,
    normalize_phone,
    string_similarity,
)
from app.matching.payer_memory import (
    find_matching_payer_aliases,
    normalize_payer_name,
    normalize_payer_reference,
)


def capture_pilot_baseline(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
) -> PilotSnapshot:
    """
    Capture a point-in-time baseline snapshot of a tenant's portfolio
    prior to or during pilot adoption.
    """
    set_tenant(session, tenant_id)
    today = datetime.now(timezone.utc).date()

    # Active (non-voided) invoices
    invoices_stmt = select(Invoice).where(
        Invoice.tenant_id == tenant_id,
        Invoice.state != InvoiceState.VOID.value,
    )
    invoices = session.execute(invoices_stmt).scalars().all()

    total_credit_sales_santim = sum(inv.amount_santim for inv in invoices)
    invoice_count = len(invoices)

    # Overdue open/partially paid
    overdue_invoices = [
        inv for inv in invoices
        if inv.state in (InvoiceState.OPEN.value, InvoiceState.PARTIALLY_PAID.value)
        and inv.due_date < today
    ]
    total_overdue_santim = sum(inv.amount_santim for inv in overdue_invoices)

    # Retailers count
    retailers_stmt = select(func.count(Retailer.id)).where(Retailer.tenant_id == tenant_id)
    retailer_count = session.execute(retailers_stmt).scalar() or 0

    snapshot = PilotSnapshot(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        snapshot_date=today,
        snapshot_type="baseline",
        total_credit_sales_santim=total_credit_sales_santim,
        total_overdue_santim=total_overdue_santim,
        invoice_count=invoice_count,
        retailer_count=retailer_count,
        metrics_payload={
            "overdue_invoice_count": len(overdue_invoices),
            "captured_at": datetime.now(timezone.utc).isoformat(),
        },
        created_by=user_id,
    )
    session.add(snapshot)
    session.flush()

    append_event(
        session,
        tenant_id=tenant_id,
        event_type="pilot.baseline_captured",
        aggregate_type="pilot_snapshot",
        aggregate_id=snapshot.id,
        actor_id=user_id,
        payload={
            "snapshot_id": str(snapshot.id),
            "snapshot_date": today.isoformat(),
            "total_credit_sales_santim": total_credit_sales_santim,
            "total_overdue_santim": total_overdue_santim,
            "invoice_count": invoice_count,
            "retailer_count": retailer_count,
        },
    )
    session.commit()
    return snapshot


def compute_pilot_metrics(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    from_date: date,
    to_date: date,
) -> dict[str, Any]:
    """
    Compute pilot audit indicators strictly from recorded system data
    over the window [from_date, to_date].
    """
    set_tenant(session, tenant_id)

    from_dt = datetime.combine(from_date, time.min, tzinfo=timezone.utc)
    to_dt = datetime.combine(to_date, time.max, tzinfo=timezone.utc)

    # 1. Log Velocity & Sequence Gaps
    # Non-voided invoices issued within date range
    inv_stmt = select(Invoice).where(
        Invoice.tenant_id == tenant_id,
        Invoice.state != InvoiceState.VOID.value,
        Invoice.issued_date >= from_date,
        Invoice.issued_date <= to_date,
    ).order_by(Invoice.issued_date, Invoice.created_at)
    invoices = session.execute(inv_stmt).scalars().all()

    inv_sample_size = len(invoices)
    if inv_sample_size > 0:
        logged_24h_count = 0
        for inv in invoices:
            issued_start = datetime.combine(inv.issued_date, time.min, tzinfo=timezone.utc)
            delta_sec = (inv.created_at.astimezone(timezone.utc) - issued_start).total_seconds()
            # Logged on issue date or next day (<= 86400 seconds)
            if 0 <= delta_sec <= 86400:
                logged_24h_count += 1
            elif (inv.created_at.astimezone(timezone.utc).date() - inv.issued_date).days <= 1:
                logged_24h_count += 1

        log_velocity_pct = round((logged_24h_count / inv_sample_size) * 100.0, 1)
        invoice_numbers = [inv.invoice_number for inv in invoices]
        sequence_gaps = detect_sequence_gaps(invoice_numbers)
        sequence_gaps_count = len(sequence_gaps)
    else:
        log_velocity_pct = None
        sequence_gaps = []
        sequence_gaps_count = None

    # 2. Collection Turnaround (days from invoice issue to confirmed payment)
    # Matches confirmed within window
    matches_stmt = (
        select(PaymentMatch, Invoice, PaymentLine)
        .join(Invoice, PaymentMatch.invoice_id == Invoice.id)
        .join(PaymentLine, PaymentMatch.payment_line_id == PaymentLine.id)
        .where(
            PaymentMatch.tenant_id == tenant_id,
            PaymentMatch.state == MatchState.CONFIRMED.value,
            PaymentMatch.confirmed_at >= from_dt,
            PaymentMatch.confirmed_at <= to_dt,
            Invoice.state != InvoiceState.VOID.value,
        )
    )
    confirmed_matches = session.execute(matches_stmt).all()
    turnaround_sample_size = len(confirmed_matches)

    if turnaround_sample_size > 0:
        turnaround_days_list: list[int] = []
        for pm, inv, pl in confirmed_matches:
            decision_date = pm.confirmed_at.astimezone(timezone.utc).date() if pm.confirmed_at else pl.value_date
            days = (decision_date - inv.issued_date).days
            turnaround_days_list.append(max(0, days))
        avg_turnaround_days = round(statistics.mean(turnaround_days_list), 1)
    else:
        avg_turnaround_days = None

    # 3. Overdue Trajectory vs Baseline
    # Current overdue open/partially paid
    overdue_stmt = select(Invoice).where(
        Invoice.tenant_id == tenant_id,
        Invoice.state.in_([InvoiceState.OPEN.value, InvoiceState.PARTIALLY_PAID.value]),
        Invoice.due_date <= to_date,
    )
    overdue_invoices = session.execute(overdue_stmt).scalars().all()
    current_overdue_santim = sum(inv.amount_santim for inv in overdue_invoices)
    overdue_sample_size = len(overdue_invoices)

    # Query latest baseline snapshot
    baseline_stmt = (
        select(PilotSnapshot)
        .where(
            PilotSnapshot.tenant_id == tenant_id,
            PilotSnapshot.snapshot_type == "baseline",
            PilotSnapshot.snapshot_date <= to_date,
        )
        .order_by(PilotSnapshot.snapshot_date.desc(), PilotSnapshot.created_at.desc())
    )
    baseline_snapshot = session.execute(baseline_stmt).scalars().first()

    if baseline_snapshot:
        baseline_overdue_santim = baseline_snapshot.total_overdue_santim
        overdue_diff_santim = current_overdue_santim - baseline_overdue_santim
        if baseline_overdue_santim > 0:
            overdue_change_pct = round(((current_overdue_santim - baseline_overdue_santim) / baseline_overdue_santim) * 100.0, 1)
        else:
            overdue_change_pct = 0.0
        baseline_sample_size = 1
    else:
        baseline_overdue_santim = None
        overdue_diff_santim = None
        overdue_change_pct = None
        baseline_sample_size = 0

    # 4. Review Queue Size & Median Time to Decision
    # Pending proposed matches
    queue_stmt = select(func.count(PaymentMatch.id)).where(
        PaymentMatch.tenant_id == tenant_id,
        PaymentMatch.state == MatchState.PROPOSED.value,
    )
    current_queue_size = session.execute(queue_stmt).scalar() or 0

    # Decided matches in window (confirmed or rejected)
    decided_matches_stmt = (
        select(PaymentMatch)
        .where(
            PaymentMatch.tenant_id == tenant_id,
            PaymentMatch.state.in_([MatchState.CONFIRMED.value, MatchState.REJECTED.value]),
            PaymentMatch.confirmed_at.isnot(None),
            PaymentMatch.confirmed_at >= from_dt,
            PaymentMatch.confirmed_at <= to_dt,
        )
    )
    decided_matches = session.execute(decided_matches_stmt).scalars().all()
    decision_sample_size = len(decided_matches)

    if decision_sample_size > 0:
        elapsed_seconds_list: list[float] = []
        for pm in decided_matches:
            if pm.confirmed_at and pm.created_at:
                sec = (pm.confirmed_at - pm.created_at).total_seconds()
                elapsed_seconds_list.append(max(0.0, sec))

        if elapsed_seconds_list:
            median_seconds = statistics.median(elapsed_seconds_list)
            if median_seconds < 3600:
                median_decision_time_str = f"{median_seconds / 60.0:.1f} minutes"
            elif median_seconds < 86400:
                median_decision_time_str = f"{median_seconds / 3600.0:.1f} hours"
            else:
                median_decision_time_str = f"{median_seconds / 86400.0:.1f} days"
        else:
            median_decision_time_str = None
    else:
        median_decision_time_str = None

    # 5. Auto-Confirm Share
    # All matches created/proposed in window
    window_matches_stmt = select(PaymentMatch).where(
        PaymentMatch.tenant_id == tenant_id,
        PaymentMatch.created_at >= from_dt,
        PaymentMatch.created_at <= to_dt,
    )
    window_matches = session.execute(window_matches_stmt).scalars().all()
    match_sample_size = len(window_matches)

    if match_sample_size > 0:
        auto_confirmed_count = sum(1 for m in window_matches if m.tier == MatchTier.TIER_A.value)
        auto_confirm_share_pct = round((auto_confirmed_count / match_sample_size) * 100.0, 1)
    else:
        auto_confirmed_count = 0
        auto_confirm_share_pct = None

    # 6. Reversal Rate (a proxy for wrong matches)
    reversals_stmt = select(Event).where(
        Event.tenant_id == tenant_id,
        Event.event_type == "match.reversed",
        Event.created_at >= from_dt,
        Event.created_at <= to_dt,
    )
    reversal_events = session.execute(reversals_stmt).scalars().all()
    reversal_count = len(reversal_events)

    if turnaround_sample_size > 0:
        reversal_rate_pct = round((reversal_count / turnaround_sample_size) * 100.0, 1)
    else:
        reversal_rate_pct = None

    # 7. Retailer Link Funnel Counts (generated, viewed, confirmed, disputed)
    link_events_stmt = select(Event).where(
        Event.tenant_id == tenant_id,
        Event.event_type.in_([
            "retailer_token.created",
            "retailer_token.viewed",
            "retailer.balance_confirmed",
            "retailer.balance_disputed",
        ]),
        Event.created_at >= from_dt,
        Event.created_at <= to_dt,
    )
    link_events = session.execute(link_events_stmt).scalars().all()

    links_generated = sum(1 for e in link_events if e.event_type == "retailer_token.created")
    links_viewed = sum(1 for e in link_events if e.event_type == "retailer_token.viewed")
    links_confirmed = sum(1 for e in link_events if e.event_type == "retailer.balance_confirmed")
    links_disputed = sum(1 for e in link_events if e.event_type == "retailer.balance_disputed")

    link_sample_size = links_generated

    # 8. Active Days of Use
    active_days_stmt = select(
        func.distinct(func.date(Event.created_at))
    ).where(
        Event.tenant_id == tenant_id,
        Event.created_at >= from_dt,
        Event.created_at <= to_dt,
    )
    active_dates = session.execute(active_days_stmt).scalars().all()
    active_days_count = len(active_dates)
    total_calendar_days = max(1, (to_date - from_date).days + 1)
    active_days_pct = round((active_days_count / total_calendar_days) * 100.0, 1)

    # 9. Real Problem Measurement: Identification Quality & Friction
    credits_stmt = select(PaymentLine).where(
        PaymentLine.tenant_id == tenant_id,
        PaymentLine.value_date >= from_date,
        PaymentLine.value_date <= to_date,
    )
    statement_credits = session.execute(credits_stmt).scalars().all()
    credit_sample_size = len(statement_credits)

    # Active / open invoices in tenant
    open_inv_stmt = select(Invoice).where(
        Invoice.tenant_id == tenant_id,
        Invoice.state.in_([InvoiceState.OPEN.value, InvoiceState.PARTIALLY_PAID.value]),
    )
    open_invoices = session.execute(open_inv_stmt).scalars().all()
    open_inv_numbers = {
        inv.invoice_number.strip().upper()
        for inv in open_invoices
        if inv.invoice_number and inv.invoice_number.strip()
    }

    # Retailers for tenant
    ret_stmt = select(Retailer).where(Retailer.tenant_id == tenant_id)
    retailers = session.execute(ret_stmt).scalars().all()

    # Active payer aliases
    alias_stmt = select(PayerAlias).where(
        PayerAlias.tenant_id == tenant_id,
        PayerAlias.deleted_at.is_(None),
    )
    active_aliases = session.execute(alias_stmt).scalars().all()

    if credit_sample_size > 0:
        ref_match_count = 0
        retailer_match_count = 0
        known_payer_count = 0
        no_identifier_count = 0

        for pl in statement_credits:
            # 1. Invoice in reference
            has_ref = False
            ref_combined = f"{pl.raw_reference or ''} {pl.source_ref or ''}".upper()
            for inv_no in open_inv_numbers:
                if inv_no in ref_combined:
                    has_ref = True
                    break

            # 2. Payer name or phone matches retailer
            has_ret = False
            cp_phone = normalize_phone(pl.counterparty_account or pl.raw_reference)
            cp_name = normalize_ethiopian_name(pl.counterparty_name) if pl.counterparty_name else None
            for r in retailers:
                if cp_phone and r.phone:
                    r_phone = normalize_phone(r.phone)
                    if r_phone and cp_phone == r_phone:
                        has_ret = True
                        break
                if cp_name and r.name:
                    r_name = normalize_ethiopian_name(r.name)
                    if cp_name == r_name or string_similarity(cp_name, r_name) >= 0.85:
                        has_ret = True
                        break

            # 3. Known payer alias match
            has_alias = False
            if active_aliases:
                line_ref = normalize_payer_reference(pl.counterparty_account or pl.raw_reference)
                line_name = normalize_payer_name(pl.counterparty_name)
                for al in active_aliases:
                    if al.normalized_payer_ref and line_ref and al.normalized_payer_ref == line_ref:
                        has_alias = True
                        break
                    if al.normalized_payer_name and line_name:
                        if al.normalized_payer_name == line_name or string_similarity(al.normalized_payer_name, line_name) >= 0.85:
                            has_alias = True
                            break

            # 4. No usable identifier
            has_no_id = (not has_ref) and (not has_ret) and (not has_alias)

            if has_ref:
                ref_match_count += 1
            if has_ret:
                retailer_match_count += 1
            if has_alias:
                known_payer_count += 1
            if has_no_id:
                no_identifier_count += 1

        share_ref_pct = round((ref_match_count / credit_sample_size) * 100.0, 1)
        share_ret_pct = round((retailer_match_count / credit_sample_size) * 100.0, 1)
        share_known_pct = round((known_payer_count / credit_sample_size) * 100.0, 1)
        share_no_id_pct = round((no_identifier_count / credit_sample_size) * 100.0, 1)
    else:
        ref_match_count = 0
        retailer_match_count = 0
        known_payer_count = 0
        no_identifier_count = 0
        share_ref_pct = None
        share_ret_pct = None
        share_known_pct = None
        share_no_id_pct = None

    # 5. Median minutes the owner spends in the review queue per day
    review_events_stmt = select(Event).where(
        Event.tenant_id == tenant_id,
        Event.event_type.in_([
            "match.web_confirmed",
            "match.web_rejected",
            "match.confirmed",
            "match.rejected",
            "match.reversed",
            "payer_alias.created",
        ]),
        Event.created_at >= from_dt,
        Event.created_at <= to_dt,
    ).order_by(Event.created_at)
    review_events = session.execute(review_events_stmt).scalars().all()

    daily_events: dict[date, list[datetime]] = {}
    for ev in review_events:
        d = ev.created_at.astimezone(timezone.utc).date()
        daily_events.setdefault(d, []).append(ev.created_at.astimezone(timezone.utc))

    daily_minutes_list: list[float] = []
    for d, timestamps in daily_events.items():
        sorted_ts = sorted(timestamps)
        day_seconds = 0.0
        prev_ts = sorted_ts[0]
        for ts in sorted_ts[1:]:
            gap = (ts - prev_ts).total_seconds()
            if gap <= 900:  # <= 15m active review clustering
                day_seconds += gap
            else:
                day_seconds += 60.0
            prev_ts = ts
        day_seconds += 60.0
        daily_minutes_list.append(round(day_seconds / 60.0, 1))

    if daily_minutes_list:
        median_daily_review_minutes = round(statistics.median(daily_minutes_list), 1)
        review_days_sample_size = len(daily_minutes_list)
        review_daily_minutes_str = f"{median_daily_review_minutes:.1f} minutes"
    else:
        median_daily_review_minutes = None
        review_days_sample_size = 0
        review_daily_minutes_str = None

    return {
        "tenant_id": str(tenant_id),
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
        "metrics": {
            "ref_invoice_match_share": {
                "name": "Reference Contains Open Invoice Number Share",
                "value": f"{share_ref_pct}%" if share_ref_pct is not None else "not available",
                "sample_size": credit_sample_size,
                "raw_pct": share_ref_pct,
                "count": ref_match_count,
            },
            "retailer_counterparty_share": {
                "name": "Payer Name / Phone Matches Retailer Share",
                "value": f"{share_ret_pct}%" if share_ret_pct is not None else "not available",
                "sample_size": credit_sample_size,
                "raw_pct": share_ret_pct,
                "count": retailer_match_count,
            },
            "known_payer_share": {
                "name": "Matched Through Known Payer Share",
                "value": f"{share_known_pct}%" if share_known_pct is not None else "not available",
                "sample_size": credit_sample_size,
                "raw_pct": share_known_pct,
                "count": known_payer_count,
            },
            "no_usable_identifier_share": {
                "name": "Payments with No Usable Identifier Share",
                "value": f"{share_no_id_pct}%" if share_no_id_pct is not None else "not available",
                "sample_size": credit_sample_size,
                "raw_pct": share_no_id_pct,
                "count": no_identifier_count,
            },
            "review_queue_daily_minutes": {
                "name": "Median Minutes Owner Spends in Review Queue per Day",
                "value": review_daily_minutes_str if review_daily_minutes_str is not None else "not available",
                "sample_size": review_days_sample_size,
                "raw_minutes": median_daily_review_minutes,
            },
            "log_velocity": {
                "name": "Log Velocity (within 24 hours)",
                "value": f"{log_velocity_pct}%" if log_velocity_pct is not None else "not available",
                "sample_size": inv_sample_size,
                "raw_pct": log_velocity_pct,
                "sequence_gaps": sequence_gaps_count if sequence_gaps_count is not None else "not available",
                "sequence_gaps_list": sequence_gaps,
            },
            "turnaround": {
                "name": "Collection Turnaround (Issue to Confirmation)",
                "value": f"{avg_turnaround_days} days" if avg_turnaround_days is not None else "not available",
                "sample_size": turnaround_sample_size,
                "raw_days": avg_turnaround_days,
            },
            "overdue_trajectory": {
                "name": "Overdue Total vs Baseline",
                "current_overdue_etb": format_money(current_overdue_santim, include_currency=True),
                "baseline_overdue_etb": format_money(baseline_overdue_santim, include_currency=True) if baseline_overdue_santim is not None else "not available",
                "change_pct": f"{overdue_change_pct:+.1f}%" if overdue_change_pct is not None else "not available",
                "value": f"{overdue_change_pct:+.1f}%" if overdue_change_pct is not None else "not available",
                "sample_size": overdue_sample_size,
                "baseline_sample_size": baseline_sample_size,
            },
            "review_queue": {
                "name": "Review Queue & Decision Velocity",
                "current_queue_depth": current_queue_size,
                "median_decision_time": median_decision_time_str if median_decision_time_str is not None else "not available",
                "value": median_decision_time_str if median_decision_time_str is not None else "not available",
                "sample_size": decision_sample_size,
            },
            "auto_confirm_share": {
                "name": "Auto-Confirm Share (Tier A)",
                "value": f"{auto_confirm_share_pct}%" if auto_confirm_share_pct is not None else "not available",
                "sample_size": match_sample_size,
                "raw_pct": auto_confirm_share_pct,
                "auto_confirmed_count": auto_confirmed_count,
            },
            "reversal_rate": {
                "name": "Reversal Rate (Proxy for Mis-matches)",
                "value": f"{reversal_rate_pct}%" if reversal_rate_pct is not None else "not available",
                "sample_size": turnaround_sample_size,
                "reversals_count": reversal_count,
            },
            "retailer_links": {
                "name": "Retailer Balance Link Funnel",
                "generated": links_generated,
                "viewed": links_viewed,
                "confirmed": links_confirmed,
                "disputed": links_disputed,
                "value": f"{links_confirmed} confirmed / {links_generated} generated" if links_generated > 0 else "not available",
                "sample_size": link_sample_size,
            },
            "active_days": {
                "name": "Active Operational Days",
                "value": f"{active_days_count} / {total_calendar_days} days ({active_days_pct}%)",
                "sample_size": total_calendar_days,
                "active_days_count": active_days_count,
                "total_calendar_days": total_calendar_days,
            },
        },
    }


def generate_pilot_metrics_html(
    report_data: dict[str, Any],
    tenant_name: str,
) -> str:
    """
    Generate an offline, self-contained local HTML document.
    Embeds all CSS and SVG; contains zero CDN links or external trackers.
    """
    m = report_data["metrics"]
    from_date = report_data["from_date"]
    to_date = report_data["to_date"]
    gen_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # Safe defaults for problem measurement metrics
    _def_m = {
        "name": "",
        "value": "not available",
        "sample_size": 0,
        "count": 0,
        "raw_pct": None,
        "raw_minutes": None,
    }
    m_ref_inv = m.get("ref_invoice_match_share", _def_m)
    m_ret_part = m.get("retailer_counterparty_share", _def_m)
    m_known_p = m.get("known_payer_share", _def_m)
    m_no_id = m.get("no_usable_identifier_share", _def_m)
    m_rev_min = m.get("review_queue_daily_minutes", _def_m)

    # Format helpers
    def _val(item: dict[str, Any]) -> str:
        v = item.get("value", "not available")
        return html.escape(str(v))

    def _badge(n: int) -> str:
        return f'<span class="sample-badge">N = {n}</span>'

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Pilot Metrics Audit Report - {html.escape(tenant_name)}</title>
  <style>
    :root {{
      --bg: #0f172a;
      --card-bg: #1e293b;
      --card-border: #334155;
      --text-main: #f8fafc;
      --text-muted: #94a3b8;
      --accent: #38bdf8;
      --success: #4ade80;
      --warning: #facc15;
      --danger: #f87171;
      --badge-bg: #0f172a;
      --font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: var(--font-family);
      background-color: var(--bg);
      color: var(--text-main);
      padding: 2rem 1.5rem;
      line-height: 1.5;
    }}
    .container {{
      max-width: 1080px;
      margin: 0 auto;
    }}
    header {{
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 12px;
      padding: 1.75rem 2rem;
      margin-bottom: 2rem;
    }}
    .header-top {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 1rem;
      margin-bottom: 0.75rem;
    }}
    h1 {{
      font-size: 1.5rem;
      font-weight: 700;
      color: var(--text-main);
    }}
    .audit-tag {{
      background: rgba(56, 189, 248, 0.15);
      color: var(--accent);
      border: 1px solid rgba(56, 189, 248, 0.3);
      padding: 0.35rem 0.85rem;
      border-radius: 9999px;
      font-size: 0.85rem;
      font-weight: 600;
    }}
    .meta-bar {{
      color: var(--text-muted);
      font-size: 0.9rem;
      display: flex;
      flex-wrap: wrap;
      gap: 1.5rem;
    }}
    .meta-bar span strong {{
      color: var(--text-main);
    }}
    .notice-box {{
      background: rgba(148, 163, 184, 0.08);
      border-left: 4px solid var(--accent);
      padding: 0.85rem 1.25rem;
      margin-top: 1rem;
      font-size: 0.85rem;
      color: var(--text-muted);
      border-radius: 4px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
      gap: 1.5rem;
      margin-bottom: 2.5rem;
    }}
    .card {{
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 12px;
      padding: 1.5rem;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
    }}
    .card-title {{
      font-size: 0.95rem;
      color: var(--text-muted);
      margin-bottom: 0.5rem;
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
    }}
    .sample-badge {{
      background: var(--badge-bg);
      border: 1px solid var(--card-border);
      color: var(--accent);
      font-family: monospace;
      font-size: 0.75rem;
      padding: 0.2rem 0.5rem;
      border-radius: 6px;
      white-space: nowrap;
    }}
    .metric-value {{
      font-size: 1.85rem;
      font-weight: 700;
      color: var(--text-main);
      margin: 0.5rem 0;
    }}
    .metric-value.na {{
      color: var(--text-muted);
      font-style: italic;
      font-size: 1.35rem;
    }}
    .card-detail {{
      font-size: 0.85rem;
      color: var(--text-muted);
      margin-top: 0.5rem;
      border-top: 1px solid rgba(255, 255, 255, 0.06);
      padding-top: 0.75rem;
    }}
    .funnel-list {{
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 0.5rem;
      text-align: center;
      margin-top: 0.75rem;
    }}
    .funnel-step {{
      background: var(--bg);
      padding: 0.5rem;
      border-radius: 6px;
      border: 1px solid var(--card-border);
    }}
    .funnel-num {{
      font-size: 1.1rem;
      font-weight: 700;
      color: var(--text-main);
    }}
    .funnel-label {{
      font-size: 0.7rem;
      color: var(--text-muted);
      text-transform: uppercase;
    }}
    footer {{
      text-align: center;
      font-size: 0.8rem;
      color: var(--text-muted);
      padding: 1.5rem 0;
      border-top: 1px solid var(--card-border);
    }}
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div class="header-top">
        <h1>Pilot Operational Metrics</h1>
        <span class="audit-tag">Privacy-Preserving Audit</span>
      </div>
      <div class="meta-bar">
        <span>Tenant: <strong>{html.escape(tenant_name)}</strong></span>
        <span>Window: <strong>{html.escape(from_date)}</strong> to <strong>{html.escape(to_date)}</strong></span>
        <span>Generated: <strong>{html.escape(gen_time)}</strong></span>
      </div>
      <div class="notice-box">
        Strict Non-Fabrication Guarantee: All figures are calculated from immutable events and database records. Missing indicators explicitly report "not available" alongside sample size (N). No customer PII or phone numbers are included.
      </div>
    </header>

    <div class="grid">
      <!-- 1. Log Velocity -->
      <div class="card">
        <div>
          <div class="card-title">
            <span>Log Velocity (within 24 hours)</span>
            {_badge(m["log_velocity"]["sample_size"])}
          </div>
          <div class="metric-value {'na' if m['log_velocity']['value'] == 'not available' else ''}">
            {_val(m["log_velocity"])}
          </div>
        </div>
        <div class="card-detail">
          Sequence Gaps: <strong>{html.escape(str(m["log_velocity"]["sequence_gaps"]))}</strong> detected
        </div>
      </div>

      <!-- 2. Collection Turnaround -->
      <div class="card">
        <div>
          <div class="card-title">
            <span>Collection Turnaround</span>
            {_badge(m["turnaround"]["sample_size"])}
          </div>
          <div class="metric-value {'na' if m['turnaround']['value'] == 'not available' else ''}">
            {_val(m["turnaround"])}
          </div>
        </div>
        <div class="card-detail">
          Average elapsed days from invoice issue to confirmed payment.
        </div>
      </div>

      <!-- 3. Overdue Trajectory vs Baseline -->
      <div class="card">
        <div>
          <div class="card-title">
            <span>Overdue Trajectory vs Baseline</span>
            {_badge(m["overdue_trajectory"]["sample_size"])}
          </div>
          <div class="metric-value {'na' if m['overdue_trajectory']['value'] == 'not available' else ''}">
            {_val(m["overdue_trajectory"])}
          </div>
        </div>
        <div class="card-detail">
          Current Overdue: <strong>{html.escape(m["overdue_trajectory"]["current_overdue_etb"])}</strong><br>
          Baseline Overdue: <strong>{html.escape(m["overdue_trajectory"]["baseline_overdue_etb"])}</strong> (N={m["overdue_trajectory"]["baseline_sample_size"]} baseline)
        </div>
      </div>

      <!-- 4. Review Queue & Median Decision Time -->
      <div class="card">
        <div>
          <div class="card-title">
            <span>Median Decision Time</span>
            {_badge(m["review_queue"]["sample_size"])}
          </div>
          <div class="metric-value {'na' if m['review_queue']['value'] == 'not available' else ''}">
            {_val(m["review_queue"])}
          </div>
        </div>
        <div class="card-detail">
          Active Review Queue Depth: <strong>{m["review_queue"]["current_queue_depth"]}</strong> pending proposals
        </div>
      </div>

      <!-- 5. Auto-Confirm Share -->
      <div class="card">
        <div>
          <div class="card-title">
            <span>Auto-Confirm Share (Tier A)</span>
            {_badge(m["auto_confirm_share"]["sample_size"])}
          </div>
          <div class="metric-value {'na' if m['auto_confirm_share']['value'] == 'not available' else ''}">
            {_val(m["auto_confirm_share"])}
          </div>
        </div>
        <div class="card-detail">
          Auto-confirmed: <strong>{m["auto_confirm_share"]["auto_confirmed_count"]}</strong> matches
        </div>
      </div>

      <!-- 6. Reversal Rate -->
      <div class="card">
        <div>
          <div class="card-title">
            <span>Reversal Rate</span>
            {_badge(m["reversal_rate"]["sample_size"])}
          </div>
          <div class="metric-value {'na' if m['reversal_rate']['value'] == 'not available' else ''}">
            {_val(m["reversal_rate"])}
          </div>
        </div>
        <div class="card-detail">
          Total Reversals: <strong>{m["reversal_rate"]["reversals_count"]}</strong> (proxy for mis-match rate)
        </div>
      </div>

      <!-- 7. Retailer Link Funnel -->
      <div class="card" style="grid-column: 1 / -1;">
        <div>
          <div class="card-title">
            <span>Retailer Confirmation Link Funnel</span>
            {_badge(m["retailer_links"]["sample_size"])}
          </div>
          <div class="funnel-list">
            <div class="funnel-step">
              <div class="funnel-num">{m["retailer_links"]["generated"]}</div>
              <div class="funnel-label">Generated</div>
            </div>
            <div class="funnel-step">
              <div class="funnel-num">{m["retailer_links"]["viewed"]}</div>
              <div class="funnel-label">Viewed</div>
            </div>
            <div class="funnel-step">
              <div class="funnel-num">{m["retailer_links"]["confirmed"]}</div>
              <div class="funnel-label">Confirmed</div>
            </div>
            <div class="funnel-step">
              <div class="funnel-num">{m["retailer_links"]["disputed"]}</div>
              <div class="funnel-label">Disputed</div>
            </div>
          </div>
        </div>
        <div class="card-detail" style="margin-top: 1rem;">
          View rate: <strong>{round((m['retailer_links']['viewed'] / m['retailer_links']['generated'] * 100), 1) if m['retailer_links']['generated'] > 0 else 0}%</strong> |
          Confirmation rate: <strong>{round((m['retailer_links']['confirmed'] / m['retailer_links']['viewed'] * 100), 1) if m['retailer_links']['viewed'] > 0 else 0}%</strong> of viewed links
        </div>
      </div>

      <!-- 8. Active Operational Days -->
      <div class="card" style="grid-column: 1 / -1;">
        <div>
          <div class="card-title">
            <span>Active Operational Days</span>
            {_badge(m["active_days"]["sample_size"])}
          </div>
          <div class="metric-value">
            {_val(m["active_days"])}
          </div>
        </div>
        <div class="card-detail">
          Calendar days with cashier or owner events recorded in window.
        </div>
      </div>

      <!-- 9. Collections Friction & Problem Measurement -->
      <div class="card" style="grid-column: 1 / -1; border-color: rgba(56, 189, 248, 0.4);">
        <div style="margin-bottom: 0.75rem;">
          <h2 style="font-size: 1.1rem; font-weight: 700; color: var(--accent); margin-bottom: 0.25rem;">
            Real Problem Measurement: Collections Friction & Remittance Quality
          </h2>
          <p style="font-size: 0.85rem; color: var(--text-muted);">
            Aggregates quantifying remittance identifier accuracy and manual queue overhead.
          </p>
        </div>
        <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 1rem; margin-top: 0.5rem;">
          <div style="background: rgba(15, 23, 42, 0.6); padding: 1rem; border-radius: 8px; border: 1px solid var(--card-border);">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem;">
              <span style="font-size: 0.8rem; color: var(--text-muted);">Invoice # in Transfer Note</span>
              {_badge(m_ref_inv["sample_size"])}
            </div>
            <div class="metric-value {'na' if m_ref_inv['value'] == 'not available' else ''}" style="font-size: 1.35rem;">
              {_val(m_ref_inv)}
            </div>
            <div class="card-detail">{m_ref_inv["count"]} statement credits</div>
          </div>

          <div style="background: rgba(15, 23, 42, 0.6); padding: 1rem; border-radius: 8px; border: 1px solid var(--card-border);">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem;">
              <span style="font-size: 0.8rem; color: var(--text-muted);">Retailer Name / Phone Match</span>
              {_badge(m_ret_part["sample_size"])}
            </div>
            <div class="metric-value {'na' if m_ret_part['value'] == 'not available' else ''}" style="font-size: 1.35rem;">
              {_val(m_ret_part)}
            </div>
            <div class="card-detail">{m_ret_part["count"]} statement credits</div>
          </div>

          <div style="background: rgba(15, 23, 42, 0.6); padding: 1rem; border-radius: 8px; border: 1px solid var(--card-border);">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem;">
              <span style="font-size: 0.8rem; color: var(--text-muted);">Known Payer Memory Match</span>
              {_badge(m_known_p["sample_size"])}
            </div>
            <div class="metric-value {'na' if m_known_p['value'] == 'not available' else ''}" style="font-size: 1.35rem;">
              {_val(m_known_p)}
            </div>
            <div class="card-detail">{m_known_p["count"]} statement credits</div>
          </div>

          <div style="background: rgba(15, 23, 42, 0.6); padding: 1rem; border-radius: 8px; border: 1px solid var(--card-border);">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem;">
              <span style="font-size: 0.8rem; color: var(--danger);">No Usable Identifier</span>
              {_badge(m_no_id["sample_size"])}
            </div>
            <div class="metric-value {'na' if m_no_id['value'] == 'not available' else ''}" style="font-size: 1.35rem; color: var(--danger);">
              {_val(m_no_id)}
            </div>
            <div class="card-detail">{m_no_id["count"]} payments unlinked</div>
          </div>

          <div style="background: rgba(15, 23, 42, 0.6); padding: 1rem; border-radius: 8px; border: 1px solid var(--card-border);">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem;">
              <span style="font-size: 0.8rem; color: var(--text-muted);">Daily Owner Review Time</span>
              {_badge(m_rev_min["sample_size"])}
            </div>
            <div class="metric-value {'na' if m_rev_min['value'] == 'not available' else ''}" style="font-size: 1.35rem;">
              {_val(m_rev_min)}
            </div>
            <div class="card-detail">Median minutes/day</div>
          </div>
        </div>
      </div>
    </div>

    <footer>
      Daily Close Operational Audit &bull; Offline Self-Contained Document &bull; Birana Technology
    </footer>
  </div>
</body>
</html>
"""
    return html_content
