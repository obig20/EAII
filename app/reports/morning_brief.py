"""
app/reports/morning_brief.py

Plain-text morning collections brief.
Ranked by days overdue and amount.
Suitable for Telegram, SMS, or plain-text console.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Sequence

from sqlalchemy.orm import Session

from app.kernel.money import to_etb
from app.reports.aging import AgingReport, generate_aging_report


def generate_morning_brief_text(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    as_of_date: date | None = None,
    max_targets: int = 10,
) -> str:
    """
    Generate plain-text morning collections brief for owner/cashier.
    Ranked by days overdue descending and amount descending.
    """
    report = generate_aging_report(session, tenant_id=tenant_id, as_of_date=as_of_date)
    today_str = (as_of_date or datetime.now(timezone.utc).date()).strftime("%d %b %Y")

    lines = [
        "========================================",
        "  DAILY CLOSE — MORNING COLLECTIONS BRIEF",
        f"  Addis Ababa · {today_str}",
        "========================================",
        f"Total Outstanding: {report.grand_total_outstanding_etb}",
        f"Total Overdue    : {report.grand_total_overdue_etb}",
        f"Current (Not Due): {to_etb(report.total_current_santim)}",
        "----------------------------------------",
        "Aging Breakdown:",
        f"  • 1 - 30 days overdue : {to_etb(report.total_1_30_santim)}",
        f"  • 31 - 60 days overdue: {to_etb(report.total_31_60_santim)}",
        f"  • 61 - 90 days overdue: {to_etb(report.total_61_90_santim)}",
        f"  • 90+ days overdue    : {to_etb(report.total_90_plus_santim)}",
        "----------------------------------------",
        "PRIORITY COLLECTION TARGETS:",
    ]

    # Filter retailers with overdue balances
    overdue_retailers = [r for r in report.retailers if r.total_overdue_santim > 0]

    if not overdue_retailers:
        lines.append("  [✓] All accounts in good standing! Zero overdue invoices.")
    else:
        # Rank by maximum overdue days descending, then total overdue amount descending
        def _rank_key(ret):
            max_days = max((inv.overdue_days for inv in ret.invoices if inv.overdue_days > 0), default=0)
            return (max_days, ret.total_overdue_santim)

        sorted_targets = sorted(overdue_retailers, key=_rank_key, reverse=True)[:max_targets]

        for idx, ret in enumerate(sorted_targets, start=1):
            phone_str = f" ({ret.phone})" if ret.phone else ""
            overdue_invs = [inv for inv in ret.invoices if inv.overdue_days > 0]
            max_days = max((inv.overdue_days for inv in overdue_invs), default=0)
            inv_nums = ", ".join(inv.invoice_number for inv in overdue_invs[:3])
            if len(overdue_invs) > 3:
                inv_nums += f" (+{len(overdue_invs) - 3} more)"

            lines.extend([
                f"{idx}. {ret.retailer_name}{phone_str}",
                f"   Due: {ret.total_overdue_etb} | Overdue: {max_days} days",
                f"   Invoices: {inv_nums}",
            ])

    lines.extend([
        "========================================",
        "Recommended action: Follow up with priority retailers by 11:00 AM.",
        "========================================",
    ])

    return "\n".join(lines)
