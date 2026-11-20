"""
app/api/v1/queries.py

Read models for /api/v1: dashboard, shop list, public balance.

Aging semantics (which invoices count, that only CONFIRMED matches reduce a
balance) come from ``app.reports.aging.generate_aging_report``; the single-
retailer balance helper below mirrors those semantics exactly so a public
balance never disagrees with the dashboard.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.v1.formatting import relative_day, santim_to_etb
from app.api.v1.schemas import DashboardOut, ShopOut
from app.kernel.enums import InvoiceState, MatchState
from app.kernel.models import Invoice, PaymentMatch, Retailer
from app.reports.aging import AgingReport, RetailerAgingSummary, generate_aging_report

TOP_OVERDUE_LIMIT = 5

# Mirrors the invoice states generate_aging_report() counts as receivable.
_RECEIVABLE_STATES = (
    InvoiceState.OPEN.value,
    InvoiceState.PARTIALLY_PAID.value,
    InvoiceState.DISPUTED.value,
)


def _max_overdue_days(summary: RetailerAgingSummary) -> int:
    return max((i.overdue_days for i in summary.invoices if i.overdue_days > 0), default=0)


def _rank_overdue(report: AgingReport) -> list[RetailerAgingSummary]:
    """Retailers with overdue invoices, by (max overdue days DESC, overdue santim DESC)."""
    overdue = [s for s in report.retailers if s.total_overdue_santim > 0]
    return sorted(
        overdue,
        key=lambda s: (-_max_overdue_days(s), -s.total_overdue_santim, s.retailer_name.lower()),
    )


def _last_sale_dates(
    session: Session, tenant_id: uuid.UUID, retailer_ids: list[uuid.UUID]
) -> dict[uuid.UUID, date]:
    if not retailer_ids:
        return {}
    rows = session.execute(
        select(Invoice.retailer_id, func.max(Invoice.issued_date))
        .where(
            Invoice.tenant_id == tenant_id,
            Invoice.retailer_id.in_(retailer_ids),
            Invoice.state != InvoiceState.VOID.value,
        )
        .group_by(Invoice.retailer_id)
    ).all()
    return {rid: last for rid, last in rows}


def _shop_out(
    *,
    retailer_id: uuid.UUID,
    name: str,
    phone: str | None,
    summary: RetailerAgingSummary | None,
    last_sale: date | None,
    today: date,
) -> ShopOut:
    return ShopOut(
        id=str(retailer_id),
        name=name,
        phone=phone or "",
        telegram="",  # retailers have no Telegram handle in the data model yet
        owed=santim_to_etb(summary.total_outstanding_santim if summary else 0),
        overdueDays=_max_overdue_days(summary) if summary else 0,
        lastSale=relative_day(last_sale, today),
    )


def build_dashboard(session: Session, tenant_id: uuid.UUID, now: datetime) -> DashboardOut:
    today = now.astimezone(timezone.utc).date()
    report = generate_aging_report(session, tenant_id=tenant_id, as_of_date=today)

    day_start = datetime.combine(today, time.min, tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)
    paid_today_santim = int(
        session.execute(
            select(func.coalesce(func.sum(PaymentMatch.matched_santim), 0)).where(
                PaymentMatch.tenant_id == tenant_id,
                PaymentMatch.state == MatchState.CONFIRMED.value,
                PaymentMatch.confirmed_at >= day_start,
                PaymentMatch.confirmed_at < day_end,
            )
        ).scalar_one()
    )

    top = _rank_overdue(report)[:TOP_OVERDUE_LIMIT]
    last_sales = _last_sale_dates(session, tenant_id, [s.retailer_id for s in top])

    return DashboardOut(
        totalOwed=santim_to_etb(report.grand_total_outstanding_santim),
        overdueTotal=santim_to_etb(report.grand_total_overdue_santim),
        paidToday=santim_to_etb(paid_today_santim),
        topOverdue=[
            _shop_out(
                retailer_id=s.retailer_id,
                name=s.retailer_name,
                phone=s.phone,
                summary=s,
                last_sale=last_sales.get(s.retailer_id),
                today=today,
            )
            for s in top
        ],
    )


def build_shop_list(session: Session, tenant_id: uuid.UUID, now: datetime) -> list[ShopOut]:
    """Every retailer for the tenant: overdue shops first, then by amount owed, then by name."""
    today = now.astimezone(timezone.utc).date()
    report = generate_aging_report(session, tenant_id=tenant_id, as_of_date=today)
    summaries = {s.retailer_id: s for s in report.retailers}

    retailers = {
        r.id: r
        for r in session.execute(select(Retailer).where(Retailer.tenant_id == tenant_id)).scalars()
    }
    last_sales = _last_sale_dates(session, tenant_id, list(retailers))

    overdue_ids = [s.retailer_id for s in _rank_overdue(report)]
    overdue_set = set(overdue_ids)
    rest = sorted(
        (r for r in retailers.values() if r.id not in overdue_set),
        key=lambda r: (
            -(summaries[r.id].total_outstanding_santim if r.id in summaries else 0),
            r.name.lower(),
        ),
    )

    ordered = [retailers[rid] for rid in overdue_ids if rid in retailers] + rest
    return [
        _shop_out(
            retailer_id=r.id,
            name=r.name,
            phone=r.phone,
            summary=summaries.get(r.id),
            last_sale=last_sales.get(r.id),
            today=today,
        )
        for r in ordered
    ]


def compute_retailer_balance(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    retailer_id: uuid.UUID,
    as_of: date,
) -> tuple[int, int]:
    """
    Return (owed_santim, overdue_santim) for one retailer.

    Only CONFIRMED matches reduce what is owed (Rule 2); proposed and
    unverified payments never do.
    """
    confirmed_paid = (
        select(
            PaymentMatch.invoice_id.label("invoice_id"),
            func.coalesce(func.sum(PaymentMatch.matched_santim), 0).label("paid_santim"),
        )
        .where(
            PaymentMatch.tenant_id == tenant_id,
            PaymentMatch.state == MatchState.CONFIRMED.value,
        )
        .group_by(PaymentMatch.invoice_id)
        .subquery()
    )
    rows = session.execute(
        select(Invoice, func.coalesce(confirmed_paid.c.paid_santim, 0))
        .outerjoin(confirmed_paid, Invoice.id == confirmed_paid.c.invoice_id)
        .where(
            Invoice.tenant_id == tenant_id,
            Invoice.retailer_id == retailer_id,
            Invoice.state.in_(_RECEIVABLE_STATES),
        )
    ).all()

    owed = overdue = 0
    for invoice, paid in rows:
        remaining = int(invoice.amount_santim) - int(paid or 0)
        if remaining <= 0:
            continue
        owed += remaining
        if invoice.due_date < as_of:
            overdue += remaining
    return owed, overdue
