"""
app/kernel/sales.py

Credit-sale capture domain logic.
Enforces:
  - Rule 1: Amounts stored as integer Santim (never float).
  - Rule 4: Append-only audit logging for invoice creation.
  - Rule 5: Invoice-agnostic: records externally issued invoices, rejects duplicates per tenant.
  - Detection and flagging of sequence gaps in invoice numbers.

Business logic in this module MUST NOT import FastAPI or aiogram.
"""

from __future__ import annotations

import csv
import io
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.importers.base import parse_amount_santim, parse_statement_date
from app.kernel.enums import InvoiceState
from app.kernel.events import append_event
from app.kernel.models import Invoice, Retailer
from app.kernel.money import validate_santim


class DuplicateInvoiceError(ValueError):
    """Raised when an invoice number already exists for this tenant."""


class InvalidSaleError(ValueError):
    """Raised when sale parameters are invalid."""


def detect_sequence_gaps(existing_numbers: Sequence[str]) -> list[str]:
    """
    Detect missing numbers in an invoice sequence.
    Groups invoice numbers by alphanumeric prefix and identifies integer gaps.
    Examples:
      ['INV-001', 'INV-002', 'INV-004'] -> ['INV-003']
      ['A1', 'A3', 'B10', 'B12'] -> ['A2', 'B11']
    """
    groups: dict[str, list[tuple[int, int, str]]] = {}

    pattern = re.compile(r"^(.*?)(\d+)$")

    for raw_num in existing_numbers:
        clean = raw_num.strip()
        m = pattern.match(clean)
        if not m:
            continue
        prefix, num_str = m.groups()
        num = int(num_str)
        width = len(num_str)
        groups.setdefault(prefix, []).append((num, width, clean))

    gaps: list[str] = []

    for prefix, items in groups.items():
        if len(items) < 2:
            continue

        items.sort(key=lambda x: x[0])
        default_width = items[0][1]

        for i in range(len(items) - 1):
            curr_num = items[i][0]
            next_num = items[i + 1][0]

            if next_num > curr_num + 1:
                # Sequence gap detected
                for missing in range(curr_num + 1, min(next_num, curr_num + 50)):
                    padded = f"{missing:0{default_width}d}"
                    gaps.append(f"{prefix}{padded}")

    return gaps


def record_credit_sale(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    invoice_number: str,
    retailer_id: uuid.UUID,
    amount_santim: int,
    issued_date: date,
    due_date: date,
) -> tuple[Invoice, list[str]]:
    """
    Record an externally issued credit sale.
    Validates amount in santim, ensures invoice_number uniqueness per tenant,
    checks for sequence gaps, and logs append-only event.
    """
    clean_inv_num = invoice_number.strip().upper()
    if not clean_inv_num:
        raise InvalidSaleError("Invoice number cannot be empty.")

    validated_santim = validate_santim(amount_santim)

    if due_date < issued_date:
        raise InvalidSaleError(f"Due date ({due_date}) cannot be earlier than issued date ({issued_date}).")

    # Check for existing duplicate invoice number for this tenant
    existing = session.execute(
        select(Invoice.id).where(
            Invoice.tenant_id == tenant_id,
            Invoice.invoice_number == clean_inv_num,
        )
    ).scalar_one_or_none()

    if existing:
        raise DuplicateInvoiceError(
            f"Invoice number {clean_inv_num!r} already exists for this tenant."
        )

    # Sequence gap detection across recent invoices
    all_numbers_res = session.execute(
        select(Invoice.invoice_number)
        .where(Invoice.tenant_id == tenant_id)
        .order_by(Invoice.created_at.desc())
        .limit(200)
    ).scalars().all()

    existing_numbers = list(all_numbers_res) + [clean_inv_num]
    gaps = detect_sequence_gaps(existing_numbers)

    now = datetime.now(timezone.utc)
    invoice = Invoice(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        invoice_number=clean_inv_num,
        retailer_id=retailer_id,
        amount_santim=validated_santim,
        issued_date=issued_date,
        due_date=due_date,
        state=InvoiceState.OPEN.value,
        created_at=now,
        created_by=user_id,
    )
    session.add(invoice)
    session.flush()

    # Append-only event (Rule 4)
    append_event(
        session,
        tenant_id=tenant_id,
        event_type="invoice.created",
        aggregate_type="invoice",
        aggregate_id=invoice.id,
        actor_id=user_id,
        payload={
            "invoice_number": clean_inv_num,
            "retailer_id": str(retailer_id),
            "amount_santim": validated_santim,
            "issued_date": issued_date.isoformat(),
            "due_date": due_date.isoformat(),
            "detected_gaps": gaps[:5] if gaps else [],
        },
    )

    return invoice, gaps


def parse_and_record_sales_csv(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    csv_content: str | bytes,
) -> tuple[list[Invoice], list[str], list[str]]:
    """
    Parse a CSV of credit sales and create invoices.
    Columns expected: Invoice Number, Retailer Name, Amount (ETB), Issue Date, Due Date, [Phone]
    Returns: (created_invoices, sequence_gaps, errors)
    """
    if isinstance(csv_content, bytes):
        text_data = csv_content.decode("utf-8-sig", errors="replace")
    else:
        text_data = csv_content

    reader = csv.DictReader(io.StringIO(text_data))
    created_invoices: list[Invoice] = []
    errors: list[str] = []

    # Cache existing retailers
    existing_retailers = session.execute(
        select(Retailer).where(Retailer.tenant_id == tenant_id)
    ).scalars().all()
    retailers_by_name = {r.name.lower().strip(): r for r in existing_retailers}

    all_gaps: list[str] = []

    for idx, row in enumerate(reader, start=2):
        inv_num = (
            row.get("Invoice Number")
            or row.get("Invoice No")
            or row.get("Invoice")
            or row.get("invoice_number", "")
        ).strip()
        ret_name = (
            row.get("Retailer Name")
            or row.get("Retailer")
            or row.get("Customer")
            or row.get("retailer_name", "")
        ).strip()
        amt_str = (
            row.get("Amount")
            or row.get("Total")
            or row.get("amount_etb")
            or row.get("amount", "")
        )
        issue_str = (
            row.get("Issue Date")
            or row.get("Date")
            or row.get("issued_date", "")
        )
        due_str = (
            row.get("Due Date")
            or row.get("due_date", "")
        )
        phone = (
            row.get("Phone")
            or row.get("Mobile")
            or row.get("phone", "")
        ).strip()

        if not inv_num or not ret_name or not amt_str:
            errors.append(f"Row {idx}: Missing required fields (Invoice Number, Retailer, Amount).")
            continue

        try:
            santim = parse_amount_santim(amt_str)
            issue_d = parse_statement_date(issue_str)
            due_d = parse_statement_date(due_str) if due_str else issue_d + (issue_d - issue_d + timedelta(days=30))
        except Exception as exc:
            errors.append(f"Row {idx} ({inv_num}): {exc}")
            continue

        # Find or create retailer
        ret_key = ret_name.lower().strip()
        retailer = retailers_by_name.get(ret_key)
        if not retailer:
            now = datetime.now(timezone.utc)
            retailer = Retailer(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                name=ret_name,
                phone=phone or None,
                created_at=now,
            )
            session.add(retailer)
            session.flush()
            retailers_by_name[ret_key] = retailer

        try:
            inv, gaps = record_credit_sale(
                session,
                tenant_id=tenant_id,
                user_id=user_id,
                invoice_number=inv_num,
                retailer_id=retailer.id,
                amount_santim=santim,
                issued_date=issue_d,
                due_date=due_d,
            )
            created_invoices.append(inv)
            all_gaps.extend(gaps)
        except (DuplicateInvoiceError, InvalidSaleError) as exc:
            errors.append(f"Row {idx} ({inv_num}): {exc}")

    return created_invoices, list(dict.fromkeys(all_gaps)), errors
