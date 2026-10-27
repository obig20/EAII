"""
app/reports/exports.py

Full CSV data export and Partner Accountant export.
Includes:
  - Full Sales Ledger (invoices, retailer, issue/due dates, amounts in Santim & ETB, status)
  - Full Payments Ledger (lines, dates, counterparty, evidence levels, confirmed matches)
  - Partner Accountant Export: reconciles sales, verified payments, and unmatched/proposed items.
"""

from __future__ import annotations

import csv
import io
import uuid
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.kernel.enums import MatchState
from app.kernel.models import Invoice, PaymentLine, PaymentMatch, Retailer
from app.kernel.money import to_etb


def sanitize_csv_cell(value: object) -> object:
    """
    Sanitize cell value to prevent CSV Formula Injection (CWE-1236).
    If a string value starts with '=', '+', '-', '@', '\t', or '\r',
    prepend a single quote (') unless it's a valid integer/decimal number
    or an international phone number (e.g. +251911000000).
    """
    if not isinstance(value, str):
        return value
    if not value:
        return value
    first_char = value[0]
    if first_char in ("=", "@", "\t", "\r"):
        return "'" + value
    if first_char == "+":
        stripped = value[1:].strip()
        cleaned = stripped.replace(" ", "").replace("-", "")
        # Phone numbers: starts with + followed by 7-15 digits (e.g. +251911000000)
        if cleaned.isdigit() and 7 <= len(cleaned) <= 15:
            return value
        # Positive numbers (e.g. +123.45)
        if stripped.isdigit() or (stripped.replace(".", "", 1).isdigit() and stripped.count(".") <= 1):
            return value
        return "'" + value
    if first_char == "-":
        stripped = value[1:].strip()
        if not (stripped.isdigit() or (stripped.replace(".", "", 1).isdigit() and stripped.count(".") <= 1)):
            return "'" + value
    return value


def sanitize_csv_row(row: Sequence[object]) -> list[object]:
    return [sanitize_csv_cell(c) for c in row]


def export_sales_csv(session: Session, *, tenant_id: uuid.UUID) -> str:
    """Generate CSV string of all sales invoices for this tenant."""
    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow(sanitize_csv_row([
        "Invoice Number",
        "Retailer Name",
        "Retailer Phone",
        "Issue Date",
        "Due Date",
        "Original Amount (Santim)",
        "Original Amount (ETB)",
        "Paid Amount (Santim)",
        "Paid Amount (ETB)",
        "Remaining Amount (Santim)",
        "Remaining Amount (ETB)",
        "State",
    ]))

    confirmed_paid_subquery = (
        select(
            PaymentMatch.invoice_id,
            func.coalesce(func.sum(PaymentMatch.matched_santim), 0).label("paid_santim"),
        )
        .where(
            PaymentMatch.tenant_id == tenant_id,
            PaymentMatch.state == MatchState.CONFIRMED.value,
        )
        .group_by(PaymentMatch.invoice_id)
        .subquery()
    )

    stmt = (
        select(
            Invoice,
            Retailer,
            func.coalesce(confirmed_paid_subquery.c.paid_santim, 0).label("paid_santim"),
        )
        .join(Retailer, Invoice.retailer_id == Retailer.id)
        .outerjoin(confirmed_paid_subquery, Invoice.id == confirmed_paid_subquery.c.invoice_id)
        .where(Invoice.tenant_id == tenant_id)
        .order_by(Invoice.issued_date.desc(), Invoice.invoice_number)
    )

    for inv, ret, paid_santim in session.execute(stmt):
        paid_int = int(paid_santim or 0)
        rem_santim = max(0, int(inv.amount_santim) - paid_int)
        writer.writerow(sanitize_csv_row([
            inv.invoice_number,
            ret.name,
            ret.phone or "",
            inv.issued_date.isoformat(),
            inv.due_date.isoformat(),
            inv.amount_santim,
            to_etb(inv.amount_santim),
            paid_int,
            to_etb(paid_int),
            rem_santim,
            to_etb(rem_santim),
            inv.state,
        ]))

    return output.getvalue()


def export_payments_csv(session: Session, *, tenant_id: uuid.UUID) -> str:
    """Generate CSV string of all imported/recorded payment lines."""
    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow([
        "Payment ID",
        "Value Date",
        "Source Ref",
        "Counterparty Name",
        "Counterparty Account",
        "Reference / Narrative",
        "Evidence Level",
        "Amount (Santim)",
        "Amount (ETB)",
        "State",
    ])

    stmt = (
        select(PaymentLine)
        .where(PaymentLine.tenant_id == tenant_id)
        .order_by(PaymentLine.value_date.desc())
    )

    for pl in session.execute(stmt).scalars():
        writer.writerow(sanitize_csv_row([
            str(pl.id),
            pl.value_date.isoformat(),
            pl.source_ref or "",
            pl.counterparty_name or "",
            pl.counterparty_account or "",
            pl.raw_reference or "",
            pl.evidence_level,
            pl.amount_santim,
            to_etb(pl.amount_santim),
            pl.state,
        ]))

    return output.getvalue()


def export_accountant_csv(session: Session, *, tenant_id: uuid.UUID) -> str:
    """
    Generate the Partner Accountant Export.
    Provides clear month-end breakdown:
      1. Sales & Collections Summary
      2. Verified Confirmed Payments
      3. Unmatched & Pending Review Items (Audit List)
    """
    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow(["# DAILY CLOSE — PARTNER ACCOUNTANT EXPORT"])
    writer.writerow(["# Tenant ID", str(tenant_id)])
    writer.writerow([])

    # SECTION 1: SALES LEDGER
    writer.writerow(["--- SECTION 1: SALES & RECEIVABLES RECONCILIATION ---"])
    writer.writerow([
        "Invoice Number", "Retailer", "Issue Date", "Due Date",
        "Original Santim", "Original ETB", "Confirmed Paid Santim", "Remaining Santim", "State",
    ])

    confirmed_paid_subquery = (
        select(
            PaymentMatch.invoice_id,
            func.coalesce(func.sum(PaymentMatch.matched_santim), 0).label("paid_santim"),
        )
        .where(
            PaymentMatch.tenant_id == tenant_id,
            PaymentMatch.state == MatchState.CONFIRMED.value,
        )
        .group_by(PaymentMatch.invoice_id)
        .subquery()
    )

    sales_stmt = (
        select(
            Invoice,
            Retailer,
            func.coalesce(confirmed_paid_subquery.c.paid_santim, 0).label("paid_santim"),
        )
        .join(Retailer, Invoice.retailer_id == Retailer.id)
        .outerjoin(confirmed_paid_subquery, Invoice.id == confirmed_paid_subquery.c.invoice_id)
        .where(Invoice.tenant_id == tenant_id)
        .order_by(Invoice.issued_date)
    )

    total_sales_santim = 0
    total_paid_santim = 0

    for inv, ret, paid_santim in session.execute(sales_stmt):
        paid_int = int(paid_santim or 0)
        rem_santim = max(0, int(inv.amount_santim) - paid_int)
        total_sales_santim += int(inv.amount_santim)
        total_paid_santim += paid_int
        writer.writerow(sanitize_csv_row([
            inv.invoice_number,
            ret.name,
            inv.issued_date.isoformat(),
            inv.due_date.isoformat(),
            inv.amount_santim,
            to_etb(inv.amount_santim),
            paid_int,
            rem_santim,
            inv.state,
        ]))

    writer.writerow([])
    writer.writerow(sanitize_csv_row(["TOTAL SALES (SANTIM)", total_sales_santim, "TOTAL SALES (ETB)", to_etb(total_sales_santim)]))
    writer.writerow(sanitize_csv_row(["TOTAL PAID (SANTIM)", total_paid_santim, "TOTAL PAID (ETB)", to_etb(total_paid_santim)]))
    writer.writerow(sanitize_csv_row(["TOTAL OUTSTANDING (SANTIM)", total_sales_santim - total_paid_santim, "TOTAL OUTSTANDING (ETB)", to_etb(total_sales_santim - total_paid_santim)]))
    writer.writerow([])

    # SECTION 2: UNMATCHED & PROPOSED ITEMS (AUDIT QUEUE)
    writer.writerow(["--- SECTION 2: UNMATCHED & PROPOSED ITEMS (AUDIT QUEUE) ---"])
    writer.writerow([
        "Payment ID", "Value Date", "Amount (Santim)", "Amount (ETB)",
        "Evidence Level", "Status", "Counterparty", "Raw Reference",
    ])

    audit_stmt = (
        select(PaymentLine)
        .where(
            PaymentLine.tenant_id == tenant_id,
            PaymentLine.state.in_(["unmatched", "proposed", "accepted_unverified"]),
        )
        .order_by(PaymentLine.value_date.desc())
    )

    unmatched_count = 0
    unmatched_santim = 0

    for pl in session.execute(audit_stmt).scalars():
        unmatched_count += 1
        unmatched_santim += pl.amount_santim
        writer.writerow(sanitize_csv_row([
            str(pl.id),
            pl.value_date.isoformat(),
            pl.amount_santim,
            to_etb(pl.amount_santim),
            pl.evidence_level,
            pl.state,
            pl.counterparty_name or pl.counterparty_account or "",
            pl.raw_reference or "",
        ]))

    writer.writerow([])
    writer.writerow(sanitize_csv_row(["TOTAL UNRESOLVED ITEMS", unmatched_count, "TOTAL UNRESOLVED SANTIM", unmatched_santim, "TOTAL UNRESOLVED ETB", to_etb(unmatched_santim)]))

    # SECTION 3: REVERSED MATCHES AUDIT LOG
    writer.writerow([])
    writer.writerow(["--- SECTION 3: REVERSED MATCHES AUDIT LOG ---"])
    writer.writerow([
        "Match ID", "Payment ID", "Invoice Number", "Retailer",
        "Reversed Santim", "Reversed ETB", "Evidence Level", "Relied on L3", "Date",
    ])

    reversals_stmt = (
        select(PaymentMatch, PaymentLine, Invoice, Retailer)
        .join(PaymentLine, PaymentMatch.payment_line_id == PaymentLine.id)
        .join(Invoice, PaymentMatch.invoice_id == Invoice.id)
        .join(Retailer, Invoice.retailer_id == Retailer.id)
        .where(
            PaymentMatch.tenant_id == tenant_id,
            PaymentMatch.state == MatchState.REVERSED.value,
        )
        .order_by(PaymentMatch.created_at.desc())
    )

    rev_count = 0
    rev_santim = 0
    for pm, pl, inv, ret in session.execute(reversals_stmt):
        rev_count += 1
        rev_santim += pm.matched_santim
        writer.writerow(sanitize_csv_row([
            str(pm.id),
            str(pl.id),
            inv.invoice_number,
            ret.name,
            pm.matched_santim,
            to_etb(pm.matched_santim),
            pl.evidence_level,
            "YES" if pl.evidence_level == "L3" else "NO",
            pm.created_at.isoformat() if pm.created_at else "",
        ]))

    writer.writerow([])
    writer.writerow(sanitize_csv_row(["TOTAL REVERSED MATCHES", rev_count, "TOTAL REVERSED SANTIM", rev_santim, "TOTAL REVERSED ETB", to_etb(rev_santim)]))

    return output.getvalue()
