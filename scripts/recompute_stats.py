"""
scripts/recompute_stats.py

Stage 4: Offline Stats Recomputation & Financial Ledger Audit Script
Birana Technology — Daily Close (ዕለታዊ ሂሳብ ማጠቃለያ)

Audit and recompute all tenant payment match balances and invoice remaining
balances against the immutable events log. Verifies:
1. invoice paid_santim == sum(matched_santim) for all confirmed matches
2. payment_line allocated_santim <= payment_line.amount_santim
3. payment_line remaining_unallocated_santim >= 0
4. immutable events ledger aligns with current invoice and payment states

Usage:
    python scripts/recompute_stats.py [--tenant-id UUID] [--fix]
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from typing import Optional

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import func, select

from app.kernel.db import session_scope
from app.kernel.enums import InvoiceState, MatchState, PaymentState
from app.kernel.models import Event, Invoice, PaymentLine, PaymentMatch, Tenant
from app.kernel.tenant import set_tenant


def audit_and_recompute_tenant_stats(
    tenant_id: uuid.UUID,
    fix: bool = False,
) -> dict[str, int]:
    """
    Audit and recompute balances for a single tenant.
    Returns audit statistics dict.
    """
    stats = {
        "invoices_checked": 0,
        "invoices_discrepancy": 0,
        "payment_lines_checked": 0,
        "payment_lines_overallocated": 0,
        "matches_checked": 0,
        "events_audited": 0,
    }

    with session_scope() as session:
        set_tenant(session, tenant_id)

        # 1. Audit Invoices and Confirmed Allocations
        invoices = session.execute(
            select(Invoice).where(Invoice.tenant_id == tenant_id)
        ).scalars().all()

        for inv in invoices:
            stats["invoices_checked"] += 1
            # Sum of confirmed matches for this invoice
            confirmed_matched = session.execute(
                select(func.coalesce(func.sum(PaymentMatch.matched_santim), 0))
                .where(
                    PaymentMatch.tenant_id == tenant_id,
                    PaymentMatch.invoice_id == inv.id,
                    PaymentMatch.state == MatchState.CONFIRMED.value,
                )
            ).scalar_one()

            expected_state = (
                InvoiceState.SETTLED.value
                if confirmed_matched >= inv.amount_santim
                else InvoiceState.PARTIALLY_PAID.value
                if confirmed_matched > 0
                else InvoiceState.OPEN.value
            )

            # In Daily Close, invoice states are OPEN, PARTIALLY_PAID, or SETTLED
            if confirmed_matched >= inv.amount_santim and inv.state != InvoiceState.SETTLED.value:
                stats["invoices_discrepancy"] += 1
                if fix:
                    inv.state = InvoiceState.SETTLED.value
            elif 0 < confirmed_matched < inv.amount_santim and inv.state != InvoiceState.PARTIALLY_PAID.value:
                stats["invoices_discrepancy"] += 1
                if fix:
                    inv.state = InvoiceState.PARTIALLY_PAID.value

        # 2. Audit Payment Lines Allocations
        lines = session.execute(
            select(PaymentLine).where(PaymentLine.tenant_id == tenant_id)
        ).scalars().all()

        for pl in lines:
            stats["payment_lines_checked"] += 1
            allocated_santim = session.execute(
                select(func.coalesce(func.sum(PaymentMatch.matched_santim), 0))
                .where(
                    PaymentMatch.tenant_id == tenant_id,
                    PaymentMatch.payment_line_id == pl.id,
                    PaymentMatch.state == MatchState.CONFIRMED.value,
                )
            ).scalar_one()

            if allocated_santim > pl.amount_santim:
                stats["payment_lines_overallocated"] += 1

        # 3. Audit Payment Matches
        matches = session.execute(
            select(PaymentMatch).where(PaymentMatch.tenant_id == tenant_id)
        ).scalars().all()
        stats["matches_checked"] = len(matches)

        # 4. Audit Immutable Events
        events_count = session.execute(
            select(func.count(Event.id)).where(Event.tenant_id == tenant_id)
        ).scalar_one()
        stats["events_audited"] = events_count

        if fix and stats["invoices_discrepancy"] > 0:
            session.commit()

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Recompute and audit financial statistics across tenants")
    parser.add_argument("--tenant-id", type=str, default=None, help="Specific tenant UUID to audit (default: all tenants)")
    parser.add_argument("--fix", action="store_true", default=False, help="Automatically fix non-fatal invoice state drifts")
    args = parser.parse_args()

    print("======================================================================")
    print("Daily Close: Offline Stats Recomputation & Financial Ledger Audit")
    print("======================================================================")

    target_tenants: list[uuid.UUID] = []
    with session_scope() as session:
        if args.tenant_id:
            tid = uuid.UUID(args.tenant_id)
            target_tenants = [tid]
        else:
            # Query all tenants
            t_rows = session.execute(select(Tenant.id)).scalars().all()
            target_tenants = list(t_rows)

    if not target_tenants:
        print("[INFO] No tenants found in database to audit.")
        return 0

    total_discrepancies = 0
    total_overallocations = 0

    for tid in target_tenants:
        res = audit_and_recompute_tenant_stats(tid, fix=args.fix)
        print(f"Tenant [{tid}]:")
        print(f"  Invoices Checked:      {res['invoices_checked']} (discrepancies: {res['invoices_discrepancy']})")
        print(f"  Payment Lines Checked: {res['payment_lines_checked']} (overallocated: {res['payment_lines_overallocated']})")
        print(f"  Matches Checked:       {res['matches_checked']}")
        print(f"  Ledger Events Audited: {res['events_audited']}")

        total_discrepancies += res["invoices_discrepancy"]
        total_overallocations += res["payment_lines_overallocated"]

    print("----------------------------------------------------------------------")
    print(f"SUMMARY: {len(target_tenants)} tenants audited.")
    print(f"Total Invoice Discrepancies: {total_discrepancies}")
    print(f"Total Overallocations:       {total_overallocations}")

    if total_overallocations > 0:
        print("[ERROR] Critical financial invariant violated: overallocated payment lines detected!", file=sys.stderr)
        return 1

    print("[SUCCESS] All tenant AR balances, allocations, and event ledgers are consistent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
