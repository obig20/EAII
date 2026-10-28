"""
app/tax/service.py

ERP Ethiopian Fiscal & Tax Compliance Service (MOR / ERCA)
Birana Technology — Daily Close ERP Suite

Implements:
1. Standard Ethiopian Tax Calculations:
   - 15% Standard VAT
   - 10% Turnover Tax (TOT)
   - 2% Withholding Tax on commercial supply transactions >= 10,000 ETB (1,000,000 santim)
2. 2% Withholding Tax receipt generation and auditing
3. Monthly Ministry of Revenues (MOR) VAT & Sales declaration CSV export
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import date, datetime, timezone
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.kernel.enums import TaxRateType
from app.kernel.events import append_event
from app.kernel.models import Invoice, Retailer, WithholdingReceipt
from app.reports.exports import sanitize_csv_row


# Ethiopian 2% Withholding Tax threshold: 10,000 ETB = 1,000,000 Santim
WITHHOLDING_THRESHOLD_SANTIM = 1_000_000


def calculate_tax_breakdown(
    base_amount_santim: int,
    tax_rate_type: TaxRateType = TaxRateType.VAT_15,
    apply_withholding: bool = False,
) -> dict[str, Any]:
    """
    Calculate Ethiopian VAT, TOT, and Withholding tax breakdown in Santim and ETB.
    """
    if base_amount_santim < 0:
        raise ValueError("Base amount must be non-negative integer santim.")

    vat_santim = 0
    tot_santim = 0

    if tax_rate_type == TaxRateType.VAT_15:
        vat_santim = int(round(base_amount_santim * 0.15))
    elif tax_rate_type == TaxRateType.TOT_10:
        tot_santim = int(round(base_amount_santim * 0.10))

    gross_with_tax = base_amount_santim + vat_santim + tot_santim

    withholding_santim = 0
    if apply_withholding and gross_with_tax >= WITHHOLDING_THRESHOLD_SANTIM:
        # Ethiopian 2% Withholding on gross taxable amount
        withholding_santim = int(round(base_amount_santim * 0.02))

    net_payable_santim = gross_with_tax - withholding_santim

    return {
        "base_amount_santim": base_amount_santim,
        "base_amount_etb": f"{base_amount_santim / 100:,.2f}",
        "tax_type": tax_rate_type.value,
        "vat_santim": vat_santim,
        "vat_etb": f"{vat_santim / 100:,.2f}",
        "tot_santim": tot_santim,
        "tot_etb": f"{tot_santim / 100:,.2f}",
        "gross_with_tax_santim": gross_with_tax,
        "gross_with_tax_etb": f"{gross_with_tax / 100:,.2f}",
        "withholding_santim": withholding_santim,
        "withholding_etb": f"{withholding_santim / 100:,.2f}",
        "net_payable_santim": net_payable_santim,
        "net_payable_etb": f"{net_payable_santim / 100:,.2f}",
    }


def issue_withholding_receipt(
    session: Session,
    tenant_id: uuid.UUID,
    receipt_number: str,
    counterparty_type: str,  # 'supplier' | 'retailer'
    counterparty_name: str,
    counterparty_tin: str,
    source_invoice_ref: str,
    gross_amount_santim: int,
    issued_date: date | None = None,
    created_by: uuid.UUID | None = None,
) -> WithholdingReceipt:
    """
    Issue an official 2% Withholding Tax receipt.
    Validates the 10,000 ETB threshold (1,000,000 santim).
    """
    if gross_amount_santim < WITHHOLDING_THRESHOLD_SANTIM:
        raise ValueError(
            f"Gross amount ({gross_amount_santim} santim = {gross_amount_santim/100:.2f} ETB) "
            f"is below the Ethiopian withholding threshold of 10,000 ETB."
        )

    withheld_santim = int(round(gross_amount_santim * 0.02))

    receipt = WithholdingReceipt(
        tenant_id=tenant_id,
        receipt_number=receipt_number.strip().upper(),
        counterparty_type=counterparty_type.strip().lower(),
        counterparty_name=counterparty_name.strip(),
        counterparty_tin=counterparty_tin.strip(),
        source_invoice_ref=source_invoice_ref.strip().upper(),
        gross_amount_santim=gross_amount_santim,
        withheld_amount_santim=withheld_santim,
        issued_date=issued_date or date.today(),
        created_by=created_by,
    )
    session.add(receipt)
    session.flush()

    append_event(
        session=session,
        tenant_id=tenant_id,
        aggregate_type="withholding_receipt",
        aggregate_id=receipt.id,
        event_type="tax.withholding_issued",
        payload={
            "receipt_number": receipt.receipt_number,
            "counterparty_tin": receipt.counterparty_tin,
            "gross_santim": gross_amount_santim,
            "withheld_santim": withheld_santim,
        },
    )
    return receipt


def generate_mor_vat_declaration_csv(
    session: Session,
    tenant_id: uuid.UUID,
    start_date: date,
    end_date: date,
) -> str:
    """
    Export monthly VAT & Sales declaration CSV matching the Ethiopian
    Ministry of Revenues (MOR / ERCA) standard layout.
    """
    stmt = (
        select(Invoice, Retailer)
        .join(Retailer, Invoice.retailer_id == Retailer.id)
        .where(
            Invoice.tenant_id == tenant_id,
            Invoice.issued_date >= start_date,
            Invoice.issued_date <= end_date,
        )
        .order_by(Invoice.issued_date)
    )

    rows = session.execute(stmt).all()

    output = io.StringIO()
    writer = csv.writer(output)

    # Header section matching Ethiopian MOR declaration
    writer.writerow(["MINISTRY OF REVENUES (MOR) - ETHIOPIA / የገቢዎች ሚኒስቴር"])
    writer.writerow(["MONTHLY VALUE ADDED TAX (VAT) SALES DECLARATION / የተጨማሪ እሴት ታክስ ሽያጭ ማስታወቂያ"])
    writer.writerow([f"Period: {start_date.isoformat()} to {end_date.isoformat()}"])
    writer.writerow([])
    writer.writerow([
        "Invoice Number",
        "Issue Date",
        "Buyer / Retailer Name",
        "Taxable Sales (ETB)",
        "VAT 15% (ETB)",
        "Gross Total (ETB)",
        "Payment State",
    ])

    total_taxable_santim = 0
    total_vat_santim = 0
    total_gross_santim = 0

    for inv, ret in rows:
        taxable_santim = inv.amount_santim
        vat_santim = int(round(taxable_santim * 0.15))
        gross_santim = taxable_santim + vat_santim

        total_taxable_santim += taxable_santim
        total_vat_santim += vat_santim
        total_gross_santim += gross_santim

        writer.writerow(sanitize_csv_row([
            inv.invoice_number,
            inv.issued_date.isoformat(),
            ret.name,
            f"{taxable_santim / 100:.2f}",
            f"{vat_santim / 100:.2f}",
            f"{gross_santim / 100:.2f}",
            inv.state,
        ]))

    writer.writerow([])
    writer.writerow(sanitize_csv_row([
        "TOTAL DECLARATION",
        "",
        f"{len(rows)} Invoices",
        f"{total_taxable_santim / 100:.2f}",
        f"{total_vat_santim / 100:.2f}",
        f"{total_gross_santim / 100:.2f}",
        "",
    ]))

    return output.getvalue()
