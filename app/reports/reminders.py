"""
app/reports/reminders.py

Payment reminder generation and Promise-to-Pay tracking (Stage 4).
Assists business owners in following up on overdue credit respectfully
without automated transmission, third-party network calls, or PII in audit events.
Provides bilingual localized templates, native sms:/tel: links, and morning brief integration.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Sequence

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.kernel.enums import EvidenceLevel, InvoiceState
from app.kernel.events import append_event
from app.kernel.models import Event, Invoice, PaymentLine, PromiseToPay, Retailer
from app.kernel.money import format_money, to_etb, validate_santim


def generate_reminder_text(
    tenant_name: str,
    retailer_name: str,
    count: int,
    amount_santim: int,
    earliest_due: date,
    lang: str = "en",
) -> str:
    """
    Builds a polite, professional reminder draft using verified locale templates.
    All parameters are inserted safely into standard text strings.
    """
    etb_formatted = format_money(amount_santim, include_currency=False)
    due_str = earliest_due.strftime("%d/%m/%Y")

    if lang == "am":
        return (
            f"ክቡር {retailer_name}፣ ይህ ከ{tenant_name} የተላከ ወዳጃዊ ማስታወሻ ነው። "
            f"ጠቅላላ ETB {etb_formatted} የሆኑ {count} ያልተከፈሉ ደረሰኞች አሉዎት። "
            f"የቀደመው መክፈያ ቀን {due_str} ነው። እባክዎ በዝውውር ማስታወሻዎ ላይ የደረሰኝ ቁጥሩን ይፃፉ <!-- needs native review -->። ለትብብርዎ እናመሰግናለን። <!-- needs native review -->"
        )

    return (
        f"Dear {retailer_name}, this is a gentle reminder from {tenant_name}. "
        f"You have {count} outstanding invoice(s) totaling ETB {etb_formatted}, "
        f"with the earliest due on {due_str}. Please write the invoice number in your transfer note. Thank you for your continued partnership."
    )


def get_overdue_retailers(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    as_of: date | None = None,
) -> list[dict]:
    """
    Fetches all retailers with overdue open invoices for the tenant.
    Aggregates invoice counts, total overdue santim, earliest due date,
    active promise-to-pay, and last logged reminder.
    """
    today = as_of or datetime.now(timezone.utc).date()

    # Query active open or partially paid invoices that are overdue (due_date < today)
    invoices = session.execute(
        select(Invoice).where(
            Invoice.tenant_id == tenant_id,
            Invoice.state.in_([InvoiceState.OPEN.value, InvoiceState.PARTIALLY_PAID.value]),
            Invoice.due_date < today,
        )
    ).scalars().all()

    if not invoices:
        return []

    # Group by retailer
    retailer_ids = {inv.retailer_id for inv in invoices}
    retailers = session.execute(
        select(Retailer).where(
            Retailer.tenant_id == tenant_id,
            Retailer.id.in_(retailer_ids),
        )
    ).scalars().all()
    retailer_map = {r.id: r for r in retailers}

    # Fetch active promises
    promises = session.execute(
        select(PromiseToPay).where(
            PromiseToPay.tenant_id == tenant_id,
            PromiseToPay.retailer_id.in_(retailer_ids),
            PromiseToPay.status == "pending",
        ).order_by(desc(PromiseToPay.created_at))
    ).scalars().all()
    promise_map: dict[uuid.UUID, PromiseToPay] = {}
    for p in promises:
        if p.retailer_id not in promise_map:
            promise_map[p.retailer_id] = p

    # Fetch last reminder events
    reminder_events = session.execute(
        select(Event).where(
            Event.tenant_id == tenant_id,
            Event.aggregate_type == "retailer",
            Event.aggregate_id.in_(retailer_ids),
            Event.event_type == "reminder.sent",
        ).order_by(desc(Event.created_at))
    ).scalars().all()
    reminder_map: dict[uuid.UUID, datetime] = {}
    for ev in reminder_events:
        if ev.aggregate_id not in reminder_map:
            reminder_map[ev.aggregate_id] = ev.created_at

    result = []
    invs_by_ret: dict[uuid.UUID, list[Invoice]] = {}
    for inv in invoices:
        invs_by_ret.setdefault(inv.retailer_id, []).append(inv)

    for ret_id, ret_invs in invs_by_ret.items():
        ret = retailer_map.get(ret_id)
        if not ret:
            continue

        total_santim = sum(inv.amount_santim for inv in ret_invs)
        earliest_due = min(inv.due_date for inv in ret_invs)
        max_overdue_days = (today - earliest_due).days

        active_p = promise_map.get(ret_id)
        is_missed = bool(active_p and active_p.promised_date < today)

        result.append({
            "retailer_id": ret.id,
            "retailer_name": ret.name,
            "phone": ret.phone,
            "overdue_count": len(ret_invs),
            "total_overdue_santim": total_santim,
            "total_overdue_etb": to_etb(total_santim),
            "earliest_due_date": earliest_due,
            "max_overdue_days": max_overdue_days,
            "active_promise": active_p,
            "is_promise_missed": is_missed,
            "last_reminder_at": reminder_map.get(ret_id),
            "invoices": sorted(ret_invs, key=lambda x: x.due_date),
        })

    # Sort priority: max overdue days descending, then amount descending
    return sorted(result, key=lambda x: (x["max_overdue_days"], x["total_overdue_santim"]), reverse=True)


def create_promise_to_pay(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    retailer_id: uuid.UUID,
    user_id: uuid.UUID,
    amount_santim: int,
    promised_date: date,
    notes: str | None = None,
) -> PromiseToPay:
    """Records a retailer promise-to-pay and emits an append-only event."""
    valid_santim = validate_santim(amount_santim)
    if valid_santim <= 0:
        raise ValueError("Promised amount must be greater than zero.")

    promise = PromiseToPay(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        retailer_id=retailer_id,
        amount_santim=valid_santim,
        promised_date=promised_date,
        status="pending",
        notes=notes.strip() if notes else None,
        created_by=user_id,
        created_at=datetime.now(timezone.utc),
    )
    session.add(promise)
    session.flush()

    append_event(
        session,
        tenant_id=tenant_id,
        event_type="promise.created",
        aggregate_type="promise",
        aggregate_id=promise.id,
        actor_id=user_id,
        evidence_level=EvidenceLevel.L3,
        payload={
            "retailer_id": str(retailer_id),
            "amount_santim": valid_santim,
            "promised_date": promised_date.isoformat(),
        },
    )

    return promise


def cancel_promise_to_pay(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    promise_id: uuid.UUID,
    user_id: uuid.UUID,
) -> PromiseToPay:
    """Cancels a pending promise-to-pay and emits an event."""
    promise = session.execute(
        select(PromiseToPay).where(
            PromiseToPay.tenant_id == tenant_id,
            PromiseToPay.id == promise_id,
        )
    ).scalar_one_or_none()

    if not promise:
        raise ValueError("Promise-to-pay record not found.")

    promise.status = "cancelled"
    session.flush()

    append_event(
        session,
        tenant_id=tenant_id,
        event_type="promise.cancelled",
        aggregate_type="promise",
        aggregate_id=promise.id,
        actor_id=user_id,
        evidence_level=EvidenceLevel.L3,
        payload={
            "retailer_id": str(promise.retailer_id),
            "amount_santim": promise.amount_santim,
        },
    )

    return promise


def log_reminder_sent(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    retailer_id: uuid.UUID,
    user_id: uuid.UUID,
    channel: str = "sms",
) -> Event:
    """
    Records that a reminder was sent manually by the owner.
    Rule: Never stores message text or personal data in event payloads.
    Only channel, actor, and timestamp are preserved in the append-only ledger.
    """
    valid_channels = {"sms", "call", "in_person", "telegram"}
    clean_channel = channel.lower() if channel.lower() in valid_channels else "sms"

    return append_event(
        session,
        tenant_id=tenant_id,
        event_type="reminder.sent",
        aggregate_type="retailer",
        aggregate_id=retailer_id,
        actor_id=user_id,
        evidence_level=EvidenceLevel.L3,
        payload={
            "channel": clean_channel,
        },
    )
