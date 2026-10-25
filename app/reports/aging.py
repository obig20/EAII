"""
app/reports/aging.py

Accounts receivable aging report.
Buckets:
  - Current (not yet due)
  - 1-30 days overdue
  - 31-60 days overdue
  - 61-90 days overdue
  - 90+ days overdue

Rules:
  - Money is integer santim (Rule 1).
  - Only CONFIRMED payments reduce remaining balance.
  - PROPOSED and ACCEPTED_UNVERIFIED payments NEVER reduce remaining balance (Rule 2).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.kernel.enums import MatchState
from app.kernel.models import Invoice, PaymentMatch, Retailer
from app.kernel.money import to_etb


@dataclass
class InvoiceAgingItem:
    invoice_id: uuid.UUID
    invoice_number: str
    retailer_name: str
    issued_date: date
    due_date: date
    overdue_days: int
    original_santim: int
    paid_santim: int
    remaining_santim: int
    bucket: str  # "current", "1_30", "31_60", "61_90", "90_plus"

    @property
    def remaining_etb(self) -> str:
        return to_etb(self.remaining_santim)


@dataclass
class RetailerAgingSummary:
    retailer_id: uuid.UUID
    retailer_name: str
    phone: str | None
    current_santim: int = 0
    days_1_30_santim: int = 0
    days_31_60_santim: int = 0
    days_61_90_santim: int = 0
    days_90_plus_santim: int = 0
    total_outstanding_santim: int = 0
    total_overdue_santim: int = 0
    invoices: list[InvoiceAgingItem] = field(default_factory=list)

    @property
    def total_outstanding_etb(self) -> str:
        return to_etb(self.total_outstanding_santim)

    @property
    def total_overdue_etb(self) -> str:
        return to_etb(self.total_overdue_santim)


@dataclass
class AgingReport:
    as_of_date: date
    total_current_santim: int = 0
    total_1_30_santim: int = 0
    total_31_60_santim: int = 0
    total_61_90_santim: int = 0
    total_90_plus_santim: int = 0
    grand_total_outstanding_santim: int = 0
    grand_total_overdue_santim: int = 0
    retailers: list[RetailerAgingSummary] = field(default_factory=list)

    @property
    def grand_total_outstanding_etb(self) -> str:
        return to_etb(self.grand_total_outstanding_santim)

    @property
    def grand_total_overdue_etb(self) -> str:
        return to_etb(self.grand_total_overdue_santim)


def calculate_aging_bucket(due_date: date, as_of: date) -> tuple[str, int]:
    """Return (bucket_name, overdue_days). overdue_days <= 0 means current."""
    overdue_days = (as_of - due_date).days
    if overdue_days <= 0:
        return "current", 0
    elif 1 <= overdue_days <= 30:
        return "1_30", overdue_days
    elif 31 <= overdue_days <= 60:
        return "31_60", overdue_days
    elif 61 <= overdue_days <= 90:
        return "61_90", overdue_days
    else:
        return "90_plus", overdue_days


def generate_aging_report(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    as_of_date: date | None = None,
) -> AgingReport:
    """
    Generate accounts receivable aging report for a tenant.
    Only confirmed payment matches deduct from the invoice amount.
    """
    as_of = as_of_date or datetime.now(timezone.utc).date()
    report = AgingReport(as_of_date=as_of)

    # 1. Fetch all confirmed payments per invoice
    confirmed_paid_subquery = (
        select(
            PaymentMatch.invoice_id,
            func.coalesce(func.sum(PaymentMatch.matched_santim), 0).label("confirmed_paid_santim"),
        )
        .where(
            PaymentMatch.tenant_id == tenant_id,
            PaymentMatch.state == MatchState.CONFIRMED.value,  # Only confirmed!
        )
        .group_by(PaymentMatch.invoice_id)
        .subquery()
    )

    # 2. Fetch invoices with remaining balance and retailer details
    stmt = (
        select(
            Invoice,
            Retailer,
            func.coalesce(confirmed_paid_subquery.c.confirmed_paid_santim, 0).label("paid_santim"),
        )
        .join(Retailer, Invoice.retailer_id == Retailer.id)
        .outerjoin(
            confirmed_paid_subquery,
            Invoice.id == confirmed_paid_subquery.c.invoice_id,
        )
        .where(
            Invoice.tenant_id == tenant_id,
            Invoice.state.in_(["open", "partially_paid", "disputed"]),
        )
        .order_by(Retailer.name, Invoice.due_date)
    )

    results = session.execute(stmt).all()

    retailers_map: dict[uuid.UUID, RetailerAgingSummary] = {}

    for invoice, retailer, paid_santim in results:
        paid_int = int(paid_santim or 0)
        remaining_santim = int(invoice.amount_santim - paid_int)
        if remaining_santim <= 0:
            continue  # Fully paid, skip

        bucket, overdue_days = calculate_aging_bucket(invoice.due_date, as_of)

        item = InvoiceAgingItem(
            invoice_id=invoice.id,
            invoice_number=invoice.invoice_number,
            retailer_name=retailer.name,
            issued_date=invoice.issued_date,
            due_date=invoice.due_date,
            overdue_days=overdue_days,
            original_santim=int(invoice.amount_santim),
            paid_santim=paid_int,
            remaining_santim=remaining_santim,
            bucket=bucket,
        )

        if retailer.id not in retailers_map:
            retailers_map[retailer.id] = RetailerAgingSummary(
                retailer_id=retailer.id,
                retailer_name=retailer.name,
                phone=retailer.phone,
            )
        ret_summary = retailers_map[retailer.id]
        ret_summary.invoices.append(item)
        ret_summary.total_outstanding_santim += remaining_santim

        if bucket == "current":
            ret_summary.current_santim += remaining_santim
            report.total_current_santim += remaining_santim
        else:
            ret_summary.total_overdue_santim += remaining_santim
            report.grand_total_overdue_santim += remaining_santim

            if bucket == "1_30":
                ret_summary.days_1_30_santim += remaining_santim
                report.total_1_30_santim += remaining_santim
            elif bucket == "31_60":
                ret_summary.days_31_60_santim += remaining_santim
                report.total_31_60_santim += remaining_santim
            elif bucket == "61_90":
                ret_summary.days_61_90_santim += remaining_santim
                report.total_61_90_santim += remaining_santim
            elif bucket == "90_plus":
                ret_summary.days_90_plus_santim += remaining_santim
                report.total_90_plus_santim += remaining_santim

        report.grand_total_outstanding_santim += remaining_santim

    # Sort retailers by total overdue descending, then total outstanding descending
    report.retailers = sorted(
        retailers_map.values(),
        key=lambda r: (r.total_overdue_santim, r.total_outstanding_santim),
        reverse=True,
    )

    return report
