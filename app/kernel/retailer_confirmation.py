"""
app/kernel/retailer_confirmation.py

Domain logic for retailer balance confirmations via signed expiring links.

Rules enforced:
  - Non-negotiable Rule 2: Evidence ladder:
      Buyer's explicit confirmation of a payment with details is L3 evidence.
      Confirming a balance without explicit payment details DOES NOT mark any
      payment confirmed.
  - Non-negotiable Rule 4: Append-only audit logging for balance confirmations and disputes.
  - Business logic MUST NEVER import FastAPI or aiogram.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.importers.base import parse_amount_santim, parse_statement_date
from app.kernel.enums import EvidenceLevel, InvoiceState, MatchState, PaymentState
from app.kernel.events import append_event
from app.kernel.models import Invoice, PaymentLine, PaymentMatch, Retailer, RetailerConfirmationToken
from app.kernel.money import to_etb, validate_santim
from app.kernel.tenant import set_tenant


class DisputeReasonCode(str, Enum):
    ALREADY_PAID = "ALREADY_PAID"
    WRONG_AMOUNT = "WRONG_AMOUNT"
    GOODS_RETURNED = "GOODS_RETURNED"
    UNRECOGNIZED = "UNRECOGNIZED"
    OTHER = "OTHER"


class TokenError(ValueError):
    """Base error for confirmation token operations."""


class TokenNotFoundError(TokenError):
    """Token does not exist."""


class TokenExpiredError(TokenError):
    """Token has expired."""


class TokenAlreadyUsedError(TokenError):
    """Token has already been used."""


def hash_token(raw_token: str) -> str:
    """Return SHA-256 hash of raw token string."""
    return hashlib.sha256(raw_token.strip().encode("utf-8")).hexdigest()


def create_retailer_confirmation_token(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    retailer_id: uuid.UUID,
    expires_in_days: int = 7,
) -> tuple[RetailerConfirmationToken, str]:
    """
    Generate a cryptographically secure signed expiring token for a retailer.
    Stores the SHA-256 hash in DB; returns (token_record, raw_token_string).
    """
    raw_token = secrets.token_urlsafe(32)
    thash = hash_token(raw_token)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=expires_in_days)

    token_record = RetailerConfirmationToken(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        retailer_id=retailer_id,
        token_hash=thash,
        expires_at=expires_at,
        used_at=None,
    )
    session.add(token_record)
    session.flush()

    append_event(
        session,
        tenant_id=tenant_id,
        event_type="retailer_token.created",
        aggregate_type="retailer_confirmation_token",
        aggregate_id=token_record.id,
        payload={
            "retailer_id": str(retailer_id),
            "expires_at": expires_at.isoformat(),
        },
    )

    return token_record, raw_token


def validate_confirmation_token(
    session: Session,
    *,
    raw_token: str,
) -> tuple[RetailerConfirmationToken, Retailer, list[Invoice]]:
    """
    Validate a raw token string against the database.
    Checks existence, expiry, and previous usage.
    Returns (token_record, retailer, list_of_open_invoices).
    """
    thash = hash_token(raw_token)
    token_record = session.execute(
        select(RetailerConfirmationToken).where(RetailerConfirmationToken.token_hash == thash)
    ).scalar_one_or_none()

    if not token_record or not hmac.compare_digest(token_record.token_hash, thash):
        raise TokenNotFoundError("Invalid confirmation link. Token not found.")

    # Immediately establish tenant context for downstream RLS queries
    set_tenant(session, token_record.tenant_id)

    now = datetime.now(timezone.utc)
    if token_record.used_at is not None:
        raise TokenAlreadyUsedError(
            f"This confirmation link was already completed on {token_record.used_at.strftime('%d %b %Y, %H:%M UTC')}."
        )

    if token_record.expires_at < now:
        raise TokenExpiredError(
            f"This confirmation link expired on {token_record.expires_at.strftime('%d %b %Y')}."
        )

    retailer = session.get(Retailer, token_record.retailer_id)
    if not retailer:
        raise TokenNotFoundError("Retailer account associated with token not found.")

    # Fetch open/partially paid invoices for this retailer
    invoices = session.execute(
        select(Invoice)
        .where(
            Invoice.tenant_id == token_record.tenant_id,
            Invoice.retailer_id == retailer.id,
            Invoice.state.in_([InvoiceState.OPEN.value, InvoiceState.PARTIALLY_PAID.value]),
        )
        .order_by(Invoice.due_date)
    ).scalars().all()

    return token_record, retailer, list(invoices)


def confirm_retailer_balance(
    session: Session,
    *,
    token_record: RetailerConfirmationToken,
    explicit_payments: list[dict[str, Any]] | None = None,
) -> tuple[bool, list[PaymentLine]]:
    """
    Retailer confirms balance.
    NON-NEGOTIABLE RULE: Confirming a balance does NOT mark any payment confirmed
    unless the retailer explicitly lists that payment.

    If explicit payment details are provided (reference, amount, date), they are recorded
    as Evidence Level L3 (Buyer's confirmation through a signed link).
    """
    now = datetime.now(timezone.utc)
    token_record.used_at = now
    session.flush()

    created_l3_payments: list[PaymentLine] = []

    if explicit_payments:
        for pdata in explicit_payments:
            amt_santim = pdata.get("amount_santim")
            if not amt_santim and "amount_etb" in pdata:
                amt_santim = parse_amount_santim(pdata["amount_etb"])

            if not amt_santim:
                continue

            v_santim = validate_santim(amt_santim)
            val_date = pdata.get("value_date") or now.date()
            if isinstance(val_date, str):
                val_date = parse_statement_date(val_date)

            pl = PaymentLine(
                id=uuid.uuid4(),
                tenant_id=token_record.tenant_id,
                source_ref=pdata.get("source_ref") or f"L3-CONF-{token_record.id.hex[:8]}",
                amount_santim=v_santim,
                value_date=val_date,
                counterparty_name=token_record.retailer.name if token_record.retailer else None,
                counterparty_account=pdata.get("counterparty_account"),
                raw_reference=pdata.get("raw_reference") or "Retailer confirmed signed-link payment",
                evidence_level=EvidenceLevel.L3.value,  # L3: buyer's confirmation through signed link!
                state=PaymentState.CONFIRMED.value,     # L3 is confirmable per Rule 2
                created_at=now,
            )
            session.add(pl)
            session.flush()
            created_l3_payments.append(pl)

            append_event(
                session,
                tenant_id=token_record.tenant_id,
                event_type="payment.l3_confirmed",
                aggregate_type="payment_line",
                aggregate_id=pl.id,
                evidence_level=EvidenceLevel.L3,
                payload={
                    "retailer_id": str(token_record.retailer_id),
                    "amount_santim": v_santim,
                    "source_ref": pl.source_ref,
                },
            )

    append_event(
        session,
        tenant_id=token_record.tenant_id,
        event_type="retailer.balance_confirmed",
        aggregate_type="retailer_confirmation_token",
        aggregate_id=token_record.id,
        payload={
            "retailer_id": str(token_record.retailer_id),
            "confirmed_at": now.isoformat(),
            "explicit_payments_count": len(created_l3_payments),
        },
    )

    return True, created_l3_payments


def dispute_retailer_balance(
    session: Session,
    *,
    token_record: RetailerConfirmationToken,
    reason_code: str,
    explanation: str,
    disputed_invoice_ids: list[uuid.UUID] | None = None,
) -> list[Invoice]:
    """
    Retailer disputes balance with a required reason code.
    Sets disputed invoices to DISPUTED state and logs append-only events.
    """
    if not reason_code:
        raise ValueError("Dispute reason code is required.")
    if not explanation or not explanation.strip():
        raise ValueError("Dispute explanation is required.")

    now = datetime.now(timezone.utc)
    token_record.used_at = now
    session.flush()

    # Query invoices to dispute
    stmt = select(Invoice).where(
        Invoice.tenant_id == token_record.tenant_id,
        Invoice.retailer_id == token_record.retailer_id,
        Invoice.state.in_([InvoiceState.OPEN.value, InvoiceState.PARTIALLY_PAID.value]),
    )
    if disputed_invoice_ids:
        stmt = stmt.where(Invoice.id.in_(disputed_invoice_ids))

    invoices = session.execute(stmt).scalars().all()
    disputed: list[Invoice] = []

    for inv in invoices:
        inv.state = InvoiceState.DISPUTED.value
        disputed.append(inv)

        append_event(
            session,
            tenant_id=token_record.tenant_id,
            event_type="invoice.disputed",
            aggregate_type="invoice",
            aggregate_id=inv.id,
            payload={
                "invoice_number": inv.invoice_number,
                "retailer_id": str(token_record.retailer_id),
                "reason_code": reason_code,
                "explanation": explanation.strip(),
                "disputed_at": now.isoformat(),
            },
        )

    append_event(
        session,
        tenant_id=token_record.tenant_id,
        event_type="retailer.balance_disputed",
        aggregate_type="retailer_confirmation_token",
        aggregate_id=token_record.id,
        payload={
            "retailer_id": str(token_record.retailer_id),
            "reason_code": reason_code,
            "explanation": explanation.strip(),
            "disputed_invoices_count": len(disputed),
        },
    )

    return disputed
