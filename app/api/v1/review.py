"""
app/api/v1/review.py

Review-queue read model and the approve / reject state changes behind
/api/v1/review-queue.

Queue item ids: the PaymentMatch id when a proposed match exists, otherwise the
PaymentLine id.

Invariants:
  Rule 2 — approval goes through ``assert_confirmable`` and fails closed for L4
           (and for any unknown evidence level).  Verification shown to clients
           is derived from the evidence level only.
  Rule 3 — callers bind the tenant first; every query here also filters by
           tenant_id explicitly.
  Rule 4 — every state change appends an event.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import and_, func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.api.v1.formatting import (
    describe_source,
    format_timestamp,
    receipt_photo_on_file,
    santim_to_etb,
    verification_for,
)
from app.api.v1.schemas import ReviewItemOut
from app.kernel.enums import EvidenceLevel, InvoiceState, MatchState, PaymentState
from app.kernel.events import append_event
from app.kernel.guards import InsufficientEvidenceError, assert_confirmable
from app.kernel.models import ImportBatch, Invoice, PaymentLine, PaymentMatch, Retailer, Tenant, User
from app.kernel.money import format_money
from app.matching.context import compute_match_reconciliation_context

_QUEUE_LINE_STATES = (PaymentState.PROPOSED.value, PaymentState.UNMATCHED.value)


def _err(code: int, error: str, message: str) -> HTTPException:
    return HTTPException(status_code=code, detail={"code": error, "message": message})


def _evidence_or_none(level: str | None) -> EvidenceLevel | None:
    try:
        return EvidenceLevel(level)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------
def list_review_items(session: Session, tenant_id: uuid.UUID, now: datetime) -> list[ReviewItemOut]:
    rows = session.execute(
        select(PaymentLine, PaymentMatch, Retailer.name, ImportBatch.source_type)
        .outerjoin(
            PaymentMatch,
            and_(
                PaymentMatch.payment_line_id == PaymentLine.id,
                PaymentMatch.tenant_id == tenant_id,
                PaymentMatch.state == MatchState.PROPOSED.value,
            ),
        )
        .outerjoin(Invoice, Invoice.id == PaymentMatch.invoice_id)
        .outerjoin(Retailer, Retailer.id == Invoice.retailer_id)
        .outerjoin(ImportBatch, ImportBatch.id == PaymentLine.import_batch_id)
        .where(
            PaymentLine.tenant_id == tenant_id,
            PaymentLine.state.in_(_QUEUE_LINE_STATES),
        )
        .order_by(PaymentLine.created_at.desc(), PaymentLine.id, PaymentMatch.id)
    ).all()

    items: list[ReviewItemOut] = []
    for pl, pm, retailer_name, source_type in rows:
        items.append(
            ReviewItemOut(
                id=str(pm.id) if pm is not None else str(pl.id),
                shopName=(retailer_name if pm is not None and retailer_name else None)
                or pl.counterparty_name
                or "Unknown payer",
                amount=santim_to_etb(pl.amount_santim),
                submittedAt=format_timestamp(pl.created_at, now),
                verification=verification_for(pl.evidence_level),
                receiptPhotoOnFile=receipt_photo_on_file(pl.evidence_level, pl.raw_reference, source_type),
                source=describe_source(pl.evidence_level, pl.raw_reference, source_type),
            )
        )
    return items


# ---------------------------------------------------------------------------
# Approve
# ---------------------------------------------------------------------------
def approve_item(
    session: Session,
    *,
    tenant: Tenant,
    user: User,
    item_id: uuid.UUID,
    confirm_differing: bool,
) -> None:
    pm = session.execute(
        select(PaymentMatch)
        .where(PaymentMatch.id == item_id, PaymentMatch.tenant_id == tenant.id)
        .with_for_update()
    ).scalar_one_or_none()

    if pm is None:
        line_exists = session.execute(
            select(PaymentLine.id).where(PaymentLine.id == item_id, PaymentLine.tenant_id == tenant.id)
        ).scalar_one_or_none()
        if line_exists is not None:
            raise _err(
                status.HTTP_409_CONFLICT,
                "no_proposed_match",
                "This payment has no proposed invoice match yet, so there is nothing to approve.",
            )
        raise _err(status.HTTP_404_NOT_FOUND, "not_found", "Review item not found.")

    if pm.state == MatchState.CONFIRMED.value:
        return  # idempotent retry
    if pm.state != MatchState.PROPOSED.value:
        raise _err(
            status.HTTP_409_CONFLICT,
            "match_not_proposed",
            f"A {pm.state} match cannot be approved.",
        )

    pl = session.execute(
        select(PaymentLine)
        .where(PaymentLine.id == pm.payment_line_id, PaymentLine.tenant_id == tenant.id)
        .with_for_update()
    ).scalar_one()

    # Rule 2: evidence ladder guard — fails closed for L4 and unknown levels.
    try:
        assert_confirmable(EvidenceLevel(pl.evidence_level))
    except (InsufficientEvidenceError, ValueError) as exc:
        raise _err(status.HTTP_400_BAD_REQUEST, "insufficient_evidence", str(exc))

    if pl.state not in _QUEUE_LINE_STATES:
        raise _err(
            status.HTTP_409_CONFLICT,
            "payment_not_reviewable",
            f"The payment is {pl.state} and can no longer be approved.",
        )

    inv = session.execute(
        select(Invoice)
        .where(Invoice.id == pm.invoice_id, Invoice.tenant_id == tenant.id)
        .with_for_update()
    ).scalar_one()
    if inv.state == InvoiceState.VOID.value:
        raise _err(status.HTTP_409_CONFLICT, "invoice_void", f"Invoice {inv.invoice_number} is void.")

    ctx = compute_match_reconciliation_context(
        session, tenant_id=tenant.id, payment_match=pm, payment_line=pl, invoice=inv
    )

    total_allocated = ctx.prior_pl_allocated_santim + ctx.matched_santim
    if total_allocated > ctx.payment_amount_santim:
        raise _err(
            status.HTTP_409_CONFLICT,
            "payment_fully_allocated",
            "This payment is already fully allocated to other invoices.",
        )
    if ctx.invoice_open_santim <= 0:
        raise _err(
            status.HTTP_409_CONFLICT,
            "invoice_already_settled",
            f"Invoice {inv.invoice_number} is already fully settled.",
        )
    if ctx.is_differing and not confirm_differing:
        raise _err(
            status.HTTP_409_CONFLICT,
            "amounts_differ_confirmation_required",
            f"Amounts differ (match {format_money(ctx.matched_santim)}, invoice open "
            f"{format_money(ctx.invoice_open_santim)}, payment {format_money(ctx.payment_amount_santim)}). "
            "Resend with confirm_differing=true to approve.",
        )

    now = datetime.now(timezone.utc)
    pm.state = MatchState.CONFIRMED.value
    pm.confirmed_by = user.id
    pm.confirmed_at = now

    pl.state = (
        PaymentState.CONFIRMED.value
        if total_allocated >= pl.amount_santim
        else PaymentState.PROPOSED.value
    )

    new_invoice_paid = (ctx.invoice_total_santim - ctx.invoice_open_santim) + pm.matched_santim
    inv.state = (
        InvoiceState.SETTLED.value
        if new_invoice_paid >= inv.amount_santim
        else InvoiceState.PARTIALLY_PAID.value
    )

    try:
        session.flush()
    except DBAPIError:
        # DB-level allocation / reversal triggers are the last line of defence.
        session.rollback()
        raise _err(
            status.HTTP_409_CONFLICT,
            "allocation_conflict",
            "The database rejected this approval because it would over-allocate the payment or invoice.",
        )

    append_event(
        session,
        tenant_id=tenant.id,
        event_type="match.api_confirmed",
        aggregate_type="payment_match",
        aggregate_id=pm.id,
        actor_id=user.id,
        evidence_level=_evidence_or_none(pl.evidence_level),
        payload={
            "invoice_id": str(inv.id),
            "payment_line_id": str(pl.id),
            "matched_santim": pm.matched_santim,
            "is_differing": ctx.is_differing,
            "channel": "api_v1",
        },
    )


# ---------------------------------------------------------------------------
# Reject
# ---------------------------------------------------------------------------
def _active_match_count(session: Session, tenant_id: uuid.UUID, line_id: uuid.UUID, *, excluding: uuid.UUID | None = None) -> int:
    stmt = select(func.count(PaymentMatch.id)).where(
        PaymentMatch.tenant_id == tenant_id,
        PaymentMatch.payment_line_id == line_id,
        PaymentMatch.state.in_([MatchState.PROPOSED.value, MatchState.CONFIRMED.value]),
    )
    if excluding is not None:
        stmt = stmt.where(PaymentMatch.id != excluding)
    return int(session.execute(stmt).scalar_one())


def reject_item(
    session: Session,
    *,
    tenant: Tenant,
    user: User,
    item_id: uuid.UUID,
) -> None:
    pm = session.execute(
        select(PaymentMatch)
        .where(PaymentMatch.id == item_id, PaymentMatch.tenant_id == tenant.id)
        .with_for_update()
    ).scalar_one_or_none()

    if pm is not None:
        if pm.state == MatchState.REJECTED.value:
            return  # idempotent retry
        if pm.state != MatchState.PROPOSED.value:
            raise _err(
                status.HTTP_409_CONFLICT,
                "match_not_proposed",
                f"A {pm.state} match cannot be rejected here; confirmed matches need a reversal.",
            )
        pl = session.execute(
            select(PaymentLine)
            .where(PaymentLine.id == pm.payment_line_id, PaymentLine.tenant_id == tenant.id)
            .with_for_update()
        ).scalar_one()

        pm.state = MatchState.REJECTED.value
        session.flush()

        # Only reject the whole payment line when nothing else is attached to it.
        line_rejected = (
            pl.state in _QUEUE_LINE_STATES
            and _active_match_count(session, tenant.id, pl.id, excluding=pm.id) == 0
        )
        if line_rejected:
            pl.state = PaymentState.REJECTED.value
            session.flush()

        append_event(
            session,
            tenant_id=tenant.id,
            event_type="match.api_rejected",
            aggregate_type="payment_match",
            aggregate_id=pm.id,
            actor_id=user.id,
            evidence_level=_evidence_or_none(pl.evidence_level),
            payload={
                "invoice_id": str(pm.invoice_id),
                "payment_line_id": str(pl.id),
                "payment_line_rejected": line_rejected,
                "channel": "api_v1",
            },
        )
        return

    pl = session.execute(
        select(PaymentLine)
        .where(PaymentLine.id == item_id, PaymentLine.tenant_id == tenant.id)
        .with_for_update()
    ).scalar_one_or_none()
    if pl is None:
        raise _err(status.HTTP_404_NOT_FOUND, "not_found", "Review item not found.")
    if pl.state == PaymentState.REJECTED.value:
        return  # idempotent retry
    if pl.state not in _QUEUE_LINE_STATES:
        raise _err(
            status.HTTP_409_CONFLICT,
            "payment_not_reviewable",
            f"The payment is {pl.state} and can no longer be rejected here.",
        )
    if _active_match_count(session, tenant.id, pl.id) > 0:
        raise _err(
            status.HTTP_409_CONFLICT,
            "payment_has_matches",
            "This payment has matches attached; reject the individual match instead.",
        )

    pl.state = PaymentState.REJECTED.value
    session.flush()
    append_event(
        session,
        tenant_id=tenant.id,
        event_type="payment_line.api_rejected",
        aggregate_type="payment_line",
        aggregate_id=pl.id,
        actor_id=user.id,
        evidence_level=_evidence_or_none(pl.evidence_level),
        payload={"amount_santim": pl.amount_santim, "channel": "api_v1"},
    )
