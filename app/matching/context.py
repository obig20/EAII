"""
app/matching/context.py

Unified reconciliation context for review cards, signed approval tokens, and approval routes.
Ensures identical amounts and differing status across card display, token signing, and approval verification.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.kernel.enums import MatchState
from app.kernel.models import Invoice, PaymentLine, PaymentMatch


@dataclass(frozen=True)
class MatchReconciliationContext:
    match_id: uuid.UUID
    payment_line_id: uuid.UUID
    invoice_id: uuid.UUID
    matched_santim: int
    invoice_open_santim: int
    invoice_total_santim: int
    payment_amount_santim: int
    prior_pl_allocated_santim: int
    remaining_pl_santim: int
    is_differing: bool
    match_kind: Literal["exact", "partial", "combined", "overpayment"]
    diff_santim: int
    unallocated_credit_santim: int


def compute_match_reconciliation_context(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    payment_match: PaymentMatch,
    payment_line: PaymentLine,
    invoice: Invoice,
) -> MatchReconciliationContext:
    """
    Compute canonical reconciliation context for a proposed payment match.
    Used by:
      1. Review queue card builder
      2. Approval token signing
      3. Match approval verification route
    """
    # 1. Invoice open balance excluding this match
    inv_confirmed_paid = session.execute(
        select(func.coalesce(func.sum(PaymentMatch.matched_santim), 0))
        .where(
            PaymentMatch.invoice_id == invoice.id,
            PaymentMatch.state == MatchState.CONFIRMED.value,
            PaymentMatch.id != payment_match.id,
        )
    ).scalar_one()
    inv_open_balance_santim = max(0, int(invoice.amount_santim) - int(inv_confirmed_paid))

    # 2. Prior confirmed allocations on this payment line excluding this match
    prior_pl_allocated = session.execute(
        select(func.coalesce(func.sum(PaymentMatch.matched_santim), 0))
        .where(
            PaymentMatch.payment_line_id == payment_line.id,
            PaymentMatch.state == MatchState.CONFIRMED.value,
            PaymentMatch.id != payment_match.id,
        )
    ).scalar_one()
    prior_pl_allocated = int(prior_pl_allocated)

    # 3. Total allocations on this payment line across confirmed + proposed
    pl_total_matched = session.execute(
        select(func.coalesce(func.sum(PaymentMatch.matched_santim), 0))
        .where(
            PaymentMatch.payment_line_id == payment_line.id,
            PaymentMatch.state.in_([MatchState.CONFIRMED.value, MatchState.PROPOSED.value]),
        )
    ).scalar_one()
    pl_total_matched = int(pl_total_matched)

    matched_santim = int(payment_match.matched_santim)
    payment_amount_santim = int(payment_line.amount_santim)
    remaining_pl_santim = max(0, payment_amount_santim - prior_pl_allocated)

    # 4. Differing amount check: matched portion != invoice open balance OR payment line total != matched portion
    is_differing = (matched_santim != inv_open_balance_santim) or (payment_amount_santim != matched_santim)

    if payment_amount_santim > matched_santim and pl_total_matched > matched_santim:
        match_kind = "combined"
        diff_santim = int(abs(payment_amount_santim - matched_santim))
        unallocated_credit_santim = max(0, payment_amount_santim - pl_total_matched)
    elif payment_amount_santim > inv_open_balance_santim:
        match_kind = "overpayment"
        diff_santim = int(payment_amount_santim - inv_open_balance_santim)
        unallocated_credit_santim = max(0, payment_amount_santim - matched_santim)
    elif matched_santim < inv_open_balance_santim:
        match_kind = "partial"
        diff_santim = int(inv_open_balance_santim - matched_santim)
        unallocated_credit_santim = 0
    else:
        match_kind = "exact"
        diff_santim = 0
        unallocated_credit_santim = 0

    return MatchReconciliationContext(
        match_id=payment_match.id,
        payment_line_id=payment_line.id,
        invoice_id=invoice.id,
        matched_santim=matched_santim,
        invoice_open_santim=inv_open_balance_santim,
        invoice_total_santim=int(invoice.amount_santim),
        payment_amount_santim=payment_amount_santim,
        prior_pl_allocated_santim=prior_pl_allocated,
        remaining_pl_santim=remaining_pl_santim,
        is_differing=is_differing,
        match_kind=match_kind,
        diff_santim=diff_santim,
        unallocated_credit_santim=unallocated_credit_santim,
    )
