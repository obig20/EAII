"""
app/channels/telegram/quick_entry.py

Quick-entry parser and processor for Telegram credit-sale capture.
Syntax format:
  "INV381 | Abebe Shop | 120000 | due 30d"
  "INV-405 | Almaz Grocery | 2500.50 ETB | due 2026-11-30"
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.importers.base import parse_amount_santim, parse_statement_date
from app.kernel.models import Invoice, Retailer
from app.kernel.money import Santim, validate_santim
from app.kernel.sales import DuplicateInvoiceError, InvalidSaleError, record_credit_sale
from app.kernel.tenant import set_tenant


@dataclass
class QuickEntryResult:
    invoice: Invoice | None
    retailer: Retailer | None
    gaps: list[str]
    amount_santim: int
    due_date: date
    error: str | None = None


def parse_quick_entry_text(
    text: str,
    base_date: date | None = None,
) -> tuple[str, str, int, date]:
    """
    Parse a single quick-entry string into:
    (invoice_number, retailer_name, amount_santim, due_date)

    Raises ValueError if syntax is invalid.
    """
    parts = [p.strip() for p in text.strip().split("|")]
    if len(parts) < 3:
        raise ValueError(
            "Expected format: 'INV-NUMBER | Retailer Name | Amount | [due Date]'\n"
            "Example: 'INV381 | Abebe Shop | 120000 | due 30d'"
        )

    inv_num = parts[0]
    retailer_name = parts[1]
    amount_raw = parts[2]
    due_raw = parts[3] if len(parts) >= 4 else "due 30d"

    if not inv_num or not retailer_name or not amount_raw:
        raise ValueError("Invoice number, Retailer name, and Amount are all required.")

    # 1. Parse Amount
    # If the user typed an integer >= 1000 without decimal (e.g. 120000 santim = 1200 ETB),
    # or decimal (e.g. 1200.00), handle standardly.
    clean_amt = amount_raw.upper().replace("ETB", "").replace("BIRR", "").replace(",", "").strip()
    if "." in clean_amt:
        santim = parse_amount_santim(clean_amt)
    else:
        # Check if pure int
        try:
            val = int(clean_amt)
            # If value looks like santim (e.g. 120000) or ETB
            santim = val
        except ValueError:
            santim = parse_amount_santim(clean_amt)

    validated_santim = validate_santim(santim)

    # 2. Parse Due Date
    today = base_date or datetime.now(timezone.utc).date()
    due_clean = due_raw.lower().replace("due", "").strip()

    days_match = re.match(r"^(\d+)\s*(?:d|days?)$", due_clean)
    if days_match:
        days_ahead = int(days_match.group(1))
        due_date = today + timedelta(days=days_ahead)
    else:
        # Try date parsing (e.g. 2026-11-25 or 25/11/2026)
        try:
            due_date = parse_statement_date(due_clean)
        except Exception:
            due_date = today + timedelta(days=30)

    return inv_num, retailer_name, validated_santim, due_date


def process_quick_entry(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    text: str,
    base_date: date | None = None,
) -> QuickEntryResult:
    """
    Parse quick-entry string, find/create retailer, and record credit sale.
    Catches domain errors (duplicate invoice number, etc.) and returns structured result.
    """
    set_tenant(session, tenant_id)
    today = base_date or datetime.now(timezone.utc).date()

    try:
        inv_num, retailer_name, santim, due_date = parse_quick_entry_text(text, base_date=today)
    except (ValueError, TypeError) as exc:
        return QuickEntryResult(
            invoice=None,
            retailer=None,
            gaps=[],
            amount_santim=0,
            due_date=today,
            error=str(exc),
        )

    # Find or create retailer
    retailer = session.execute(
        select(Retailer).where(
            Retailer.tenant_id == tenant_id,
            func_lower(Retailer.name) == retailer_name.lower().strip(),
        )
    ).scalar_one_or_none()

    if not retailer:
        now = datetime.now(timezone.utc)
        retailer = Retailer(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            name=retailer_name.strip(),
            created_at=now,
        )
        session.add(retailer)
        session.flush()

    try:
        invoice, gaps = record_credit_sale(
            session,
            tenant_id=tenant_id,
            user_id=user_id,
            invoice_number=inv_num,
            retailer_id=retailer.id,
            amount_santim=santim,
            issued_date=today,
            due_date=due_date,
        )
        return QuickEntryResult(
            invoice=invoice,
            retailer=retailer,
            gaps=gaps,
            amount_santim=santim,
            due_date=due_date,
            error=None,
        )
    except (DuplicateInvoiceError, InvalidSaleError, ValueError, TypeError) as exc:
        return QuickEntryResult(
            invoice=None,
            retailer=retailer,
            gaps=[],
            amount_santim=santim,
            due_date=due_date,
            error=str(exc),
        )


def func_lower(col):
    from sqlalchemy import func
    return func.lower(col)
