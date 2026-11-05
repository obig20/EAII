"""
app/reports/health_check.py

Collections Health Check diagnostic engine for prospective business owners.
Analyzes an owner's historical unpaid invoices and bank/mobile money statements.

Computes:
  1. Receivables aging breakdown across 5 standard aging buckets.
  2. Top 10 largest overdue retailers (with phone and estimated days-to-collect DSO).
  3. Statement reconciliation health (auto-confirmed vs. queued review vs. unmatched).
  4. Possible duplicate payments, overpayments, and unallocated credits.
  5. Invoiced sequence gaps (missing sales slip numbers).
  6. Unidentified bank deposits (inflows with no corresponding invoice).
  7. Estimated average days-to-collect (DSO) per retailer.
  8. Prioritized, plain-language 'Fix These First' executive action list.

Zero network calls, 100% offline, integer santim only, unprivileged role compatible.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.importers.base import (
    BaseImporter,
    StatementImportError,
    parse_amount_santim,
    parse_statement_date,
)
from app.importers.csv_importer import CsvStatementImporter
from app.importers.mapping import (
    CBE_CSV_CONFIG,
    ColumnMappingConfig,
    TELEBIRR_XLSX_CONFIG,
)
from app.importers.xlsx_importer import XlsxStatementImporter
from app.kernel.db import _SessionLocal
from app.kernel.enums import (
    EvidenceLevel,
    InvoiceState,
    MatchState,
    MatchTier,
    PaymentState,
    UserRole,
)
from app.kernel.events import append_event
from app.kernel.models import (
    ImportBatch,
    Invoice,
    PaymentLine,
    PaymentMatch,
    Retailer,
    Tenant,
    User,
)
from app.kernel.money import to_etb, validate_santim
from app.kernel.sales import (
    DuplicateInvoiceError,
    InvalidSaleError,
    detect_sequence_gaps,
    record_credit_sale,
)
from app.kernel.tenant import set_tenant
from app.matching.matcher import PaymentMatcher
from app.reports.aging import calculate_aging_bucket, generate_aging_report
from app.reports.health_check_html import render_collections_health_check_html

logger = logging.getLogger(__name__)


class FriendlyMappingError(ValueError):
    """Raised when columns cannot be recognized and user needs clear guidance."""
    pass


# ---------------------------------------------------------------------------
# Column Auto-Detection & Mapping Helpers
# ---------------------------------------------------------------------------

INVOICE_NUM_ALIASES = [
    "invoice number", "invoice no", "invoice_no", "invoice #", "invoice#",
    "invoice", "inv no", "inv_num", "bill no", "bill_no", "slip no", "slip_no",
]
RETAILER_NAME_ALIASES = [
    "retailer name", "retailer", "customer name", "customer", "client name",
    "client", "buyer name", "buyer", "merchant", "retailer_name",
]
AMOUNT_ALIASES = [
    "amount", "total", "amount (etb)", "amount_etb", "amount etb", "total (etb)",
    "price", "balance", "net amount", "total_amount",
]
ISSUE_DATE_ALIASES = [
    "issue date", "date", "invoice date", "issued_date", "inv date",
    "sales date", "posting date",
]
DUE_DATE_ALIASES = [
    "due date", "due_date", "payment due", "maturity date",
]
PHONE_ALIASES = [
    "phone", "mobile", "phone number", "mobile number", "contact", "tel",
]


def _match_column(headers: Sequence[str], aliases: Sequence[str]) -> str | None:
    """Find matching column name from headers ignoring case and spaces."""
    header_map = {re.sub(r"[\s_\-#]+", " ", h.strip().lower()): h for h in headers if h}
    for alias in aliases:
        norm_alias = re.sub(r"[\s_\-#]+", " ", alias.lower())
        if norm_alias in header_map:
            return header_map[norm_alias]
    return None


def resolve_invoice_mapping(
    headers: list[str],
    file_path: str,
    custom_map: dict[str, Any] | None = None,
) -> dict[str, str]:
    """
    Resolve column mapping for invoices CSV.
    Raises FriendlyMappingError if required columns cannot be found.
    """
    inv_map = {}
    if custom_map:
        inv_map = custom_map.get("invoices", custom_map)

    inv_num_col = inv_map.get("invoice_number") or _match_column(headers, INVOICE_NUM_ALIASES)
    ret_name_col = inv_map.get("retailer_name") or _match_column(headers, RETAILER_NAME_ALIASES)
    amt_col = inv_map.get("amount") or _match_column(headers, AMOUNT_ALIASES)
    issue_date_col = inv_map.get("issue_date") or _match_column(headers, ISSUE_DATE_ALIASES)
    due_date_col = inv_map.get("due_date") or _match_column(headers, DUE_DATE_ALIASES)
    phone_col = inv_map.get("phone") or _match_column(headers, PHONE_ALIASES)

    missing = []
    if not inv_num_col or inv_num_col not in headers:
        missing.append("Invoice Number")
    if not ret_name_col or ret_name_col not in headers:
        missing.append("Retailer Name")
    if not amt_col or amt_col not in headers:
        missing.append("Amount")

    if missing:
        headers_preview = ", ".join(repr(h) for h in sorted(headers)) if headers else "(empty)"
        msg = (
            f"Error: Invoices file '{file_path}' has unrecognized column headers:\n"
            f"  Detected columns: [{headers_preview}]\n"
            f"  Missing required fields: {', '.join(missing)}\n\n"
            f"Please provide a JSON column mapping file using --map <mapping.json>.\n"
            f"Example map structure:\n"
            f'{{\n'
            f'  "invoices": {{\n'
            f'    "invoice_number": "{headers[0] if headers else "Invoice_No"}",\n'
            f'    "retailer_name": "{headers[1] if len(headers) > 1 else "Customer"}",\n'
            f'    "amount": "{headers[2] if len(headers) > 2 else "Total"}"\n'
            f'  }}\n'
            f'}}'
        )
        raise FriendlyMappingError(msg)

    return {
        "invoice_number": inv_num_col,
        "retailer_name": ret_name_col,
        "amount": amt_col,
        "issue_date": issue_date_col,
        "due_date": due_date_col,
        "phone": phone_col,
    }


def resolve_statement_mapping(
    headers: list[str],
    file_path: str,
    custom_map: dict[str, Any] | None = None,
) -> ColumnMappingConfig:
    """
    Resolve column mapping for statement CSV or XLSX.
    Raises FriendlyMappingError if required columns cannot be found.
    """
    stmt_map = {}
    if custom_map:
        stmt_map = custom_map.get("statements", custom_map)

    # Check custom map
    if "date_column" in stmt_map and "amount_column" in stmt_map:
        d_col = stmt_map["date_column"]
        a_col = stmt_map["amount_column"]
        if d_col in headers and a_col in headers:
            return ColumnMappingConfig.from_dict({
                "source_name": "custom_statement",
                "evidence_level": stmt_map.get("evidence_level", "L2"),
                "date_column": d_col,
                "date_format": stmt_map.get("date_format", "%Y-%m-%d"),
                "amount_column": a_col,
                "reference_column": stmt_map.get("reference_column"),
                "narrative_column": stmt_map.get("narrative_column"),
                "counterparty_name_column": stmt_map.get("counterparty_name_column"),
                "counterparty_account_column": stmt_map.get("counterparty_account_column"),
                "tx_id_column": stmt_map.get("tx_id_column"),
                "polarity_column": stmt_map.get("polarity_column"),
                "credit_value": stmt_map.get("credit_value"),
            })

    # Auto-detect known configs
    header_set = {h.strip().lower() for h in headers if h}

    # CBE format: 'Posting Date' and ('Credit Amount' or 'Amount')
    if "posting date" in header_set and any(x in header_set for x in ("credit amount", "credit", "amount")):
        cbe_dict = CBE_CSV_CONFIG.to_dict()
        for k in ["date_column", "amount_column", "reference_column", "narrative_column",
                  "counterparty_name_column", "counterparty_account_column", "tx_id_column"]:
            matched = _match_column(headers, [cbe_dict[k]]) if cbe_dict.get(k) else None
            if matched:
                cbe_dict[k] = matched
        return ColumnMappingConfig.from_dict(cbe_dict)

    # Telebirr XLSX format
    if "transaction time" in header_set and "net amount" in header_set:
        tb_dict = TELEBIRR_XLSX_CONFIG.to_dict()
        return ColumnMappingConfig.from_dict(tb_dict)

    # Generic search for Date and Amount
    date_col = _match_column(headers, [
        "value date", "posting date", "transaction date", "txn date", "date",
        "trans date", "payment date", "booking date",
    ])
    amt_col = _match_column(headers, [
        "credit amount", "credit", "net amount", "amount (etb)", "amount",
        "paid in", "deposit", "inflow", "cr amount",
    ])
    ref_col = _match_column(headers, [
        "reference no", "reference", "ref no", "txn ref", "narration",
        "remarks", "description", "transaction details",
    ])
    narr_col = _match_column(headers, [
        "description", "narrative", "narration", "service name", "transaction details",
    ])
    cp_name_col = _match_column(headers, [
        "counterparty name", "customer name", "sender name", "payer name", "name",
    ])
    cp_acc_col = _match_column(headers, [
        "counterparty account", "source account", "sender mobile", "account", "mobile",
    ])
    tx_col = _match_column(headers, [
        "transaction id", "txn id", "receipt no", "reference no", "journal no",
    ])

    missing = []
    if not date_col or date_col not in headers:
        missing.append("Date")
    if not amt_col or amt_col not in headers:
        missing.append("Amount (Credit)")

    if missing:
        headers_preview = ", ".join(repr(h) for h in sorted(headers)) if headers else "(empty)"
        msg = (
            f"Error: Statement file '{file_path}' has unrecognized column headers:\n"
            f"  Detected columns: [{headers_preview}]\n"
            f"  Missing required fields: {', '.join(missing)}\n\n"
            f"Please provide a JSON column mapping file using --map <mapping.json>.\n"
            f"Example map structure:\n"
            f'{{\n'
            f'  "statements": {{\n'
            f'    "date_column": "{headers[0] if headers else "Date"}",\n'
            f'    "amount_column": "{headers[1] if len(headers) > 1 else "Credit"}",\n'
            f'    "reference_column": "{headers[2] if len(headers) > 2 else "Narration"}"\n'
            f'  }}\n'
            f'}}'
        )
        raise FriendlyMappingError(msg)

    return ColumnMappingConfig(
        source_name="auto_detected_statement",
        evidence_level=EvidenceLevel.L2,
        date_column=date_col,
        amount_column=amt_col,
        reference_column=ref_col,
        narrative_column=narr_col,
        counterparty_name_column=cp_name_col,
        counterparty_account_column=cp_acc_col,
        tx_id_column=tx_col,
    )


# ---------------------------------------------------------------------------
# Core Diagnostic Execution
# ---------------------------------------------------------------------------

def run_health_check(
    *,
    invoices_path: str | Path,
    statements_paths: Sequence[str | Path],
    out_path: str | Path = "report.html",
    lang: str = "en",
    map_path: str | Path | None = None,
    keep: bool = False,
    as_of_date: date | None = None,
) -> dict[str, Any]:
    """
    Execute Collections Health Check and write self-contained HTML report.
    """
    invoices_p = Path(invoices_path)
    if not invoices_p.is_file():
        raise FileNotFoundError(f"Invoices CSV not found: {invoices_p}")

    stmt_paths = [Path(p) for p in statements_paths]
    for p in stmt_paths:
        if not p.is_file():
            raise FileNotFoundError(f"Statement file not found: {p}")

    # Load custom mapping if provided
    custom_map: dict[str, Any] | None = None
    if map_path:
        mp = Path(map_path)
        if not mp.is_file():
            raise FileNotFoundError(f"Mapping JSON file not found: {mp}")
        with open(mp, "r", encoding="utf-8") as f:
            custom_map = json.load(f)

    session = _SessionLocal()
    temp_tenant_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    analysis_date = as_of_date or now.date()

    try:
        # 1. Provision isolated temporary tenant
        temp_tenant = Tenant(
            id=temp_tenant_id,
            name=f"HealthCheck-Prospect-{str(temp_tenant_id)[:8]}",
            is_demo=True,
            created_at=now,
        )
        session.add(temp_tenant)
        session.flush()

        set_tenant(session, temp_tenant.id)

        # Create temporary owner user as actor for audit events
        owner_user = User(
            id=uuid.uuid4(),
            tenant_id=temp_tenant.id,
            role=UserRole.OWNER.value,
            display_name="Health Check Auditor",
            email=f"auditor-{str(temp_tenant_id)[:8]}@birana.et",
            is_active=True,
            created_at=now,
        )
        session.add(owner_user)
        session.flush()

        # 2. Ingest Invoices
        with open(invoices_p, "r", encoding="utf-8-sig", errors="replace") as f:
            inv_text = f.read()

        sample = inv_text[:2048]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            delim = dialect.delimiter
        except Exception:
            delim = ","

        inv_reader = csv.DictReader(io.StringIO(inv_text), delimiter=delim)
        raw_inv_headers = inv_reader.fieldnames or []
        inv_col_map = resolve_invoice_mapping(raw_inv_headers, str(invoices_p), custom_map)

        retailers_by_name: dict[str, Retailer] = {}
        created_invoices: list[Invoice] = []
        raw_invoice_numbers: list[str] = []

        for row_idx, row in enumerate(inv_reader, start=2):
            raw_inv_num = str(row.get(inv_col_map["invoice_number"], "")).strip()
            raw_ret_name = str(row.get(inv_col_map["retailer_name"], "")).strip()
            raw_amt = row.get(inv_col_map["amount"])

            if not raw_inv_num or not raw_ret_name or raw_amt is None or str(raw_amt).strip() == "":
                continue

            try:
                amt_santim = parse_amount_santim(raw_amt)
            except Exception:
                continue

            # Parse dates
            raw_issue = row.get(inv_col_map["issue_date"]) if inv_col_map.get("issue_date") else None
            try:
                issue_d = parse_statement_date(raw_issue) if raw_issue else analysis_date
            except Exception:
                issue_d = analysis_date

            raw_due = row.get(inv_col_map["due_date"]) if inv_col_map.get("due_date") else None
            try:
                due_d = parse_statement_date(raw_due) if raw_due else issue_d + timedelta(days=30)
            except Exception:
                due_d = issue_d + timedelta(days=30)

            raw_phone = str(row.get(inv_col_map["phone"], "")).strip() if inv_col_map.get("phone") else None

            # Resolve retailer
            ret_key = raw_ret_name.lower().strip()
            if ret_key not in retailers_by_name:
                ret = Retailer(
                    id=uuid.uuid4(),
                    tenant_id=temp_tenant.id,
                    name=raw_ret_name,
                    phone=raw_phone or None,
                    created_at=now,
                )
                session.add(ret)
                session.flush()
                retailers_by_name[ret_key] = ret
            else:
                ret = retailers_by_name[ret_key]

            try:
                inv, _ = record_credit_sale(
                    session,
                    tenant_id=temp_tenant.id,
                    user_id=owner_user.id,
                    invoice_number=raw_inv_num,
                    retailer_id=ret.id,
                    amount_santim=amt_santim,
                    issued_date=issue_d,
                    due_date=due_d,
                )
                created_invoices.append(inv)
                raw_invoice_numbers.append(raw_inv_num)
            except (DuplicateInvoiceError, InvalidSaleError):
                continue

        # 3. Ingest Statements
        imported_payment_lines: list[PaymentLine] = []
        for stmt_p in stmt_paths:
            is_xlsx = stmt_p.suffix.lower() == ".xlsx"
            if is_xlsx:
                xlsx_importer = XlsxStatementImporter(TELEBIRR_XLSX_CONFIG)
                raw_rows = xlsx_importer.extract_raw_rows(stmt_p)
                headers = list(raw_rows[0].keys()) if raw_rows else []
                cfg = resolve_statement_mapping(headers, str(stmt_p), custom_map)
                xlsx_importer.config = cfg
                batch, lines = xlsx_importer.import_statement(
                    session,
                    tenant_id=temp_tenant.id,
                    user_id=owner_user.id,
                    content_or_path=stmt_p,
                    filename=stmt_p.name,
                )
                imported_payment_lines.extend(lines)
            else:
                csv_importer = CsvStatementImporter(CBE_CSV_CONFIG)
                raw_rows = csv_importer.extract_raw_rows(stmt_p)
                headers = list(raw_rows[0].keys()) if raw_rows else []
                cfg = resolve_statement_mapping(headers, str(stmt_p), custom_map)
                csv_importer.config = cfg
                batch, lines = csv_importer.import_statement(
                    session,
                    tenant_id=temp_tenant.id,
                    user_id=owner_user.id,
                    content_or_path=stmt_p,
                    filename=stmt_p.name,
                )
                imported_payment_lines.extend(lines)

        # 4. Deterministic Reconciliation Engine
        matcher = PaymentMatcher()
        retailers_list = list(retailers_by_name.values())
        auto_confirmed_matches: list[tuple[PaymentLine, PaymentMatch]] = []
        queued_proposed_matches: list[tuple[PaymentLine, PaymentMatch]] = []
        unmatched_lines: list[PaymentLine] = []

        # Keep open invoices in memory
        invoices_by_id = {inv.id: inv for inv in created_invoices}

        for pl in imported_payment_lines:
            active_invoices = [inv for inv in created_invoices if inv.state in ("open", "partially_paid")]
            res = matcher.match_payment(pl, active_invoices, retailers_list)

            if res.status == "auto_confirmed" and res.matches:
                target_item = res.matches[0]
                target_inv = invoices_by_id[target_item.invoice_id]
                match_id = uuid.uuid4()
                pm = PaymentMatch(
                    id=match_id,
                    tenant_id=temp_tenant.id,
                    payment_line_id=pl.id,
                    invoice_id=target_inv.id,
                    matched_santim=target_item.matched_santim,
                    tier=MatchTier.TIER_A.value,
                    state=MatchState.CONFIRMED.value,
                    confirmed_by=owner_user.id,
                    confirmed_at=now,
                    created_at=now,
                )
                session.add(pm)
                pl.state = PaymentState.CONFIRMED.value

                # Update invoice status
                target_inv.state = InvoiceState.SETTLED.value
                auto_confirmed_matches.append((pl, pm))

            elif res.status == "proposed" and res.matches:
                for target_item in res.matches[:2]:  # up to 2 candidates
                    target_inv = invoices_by_id[target_item.invoice_id]
                    pm = PaymentMatch(
                        id=uuid.uuid4(),
                        tenant_id=temp_tenant.id,
                        payment_line_id=pl.id,
                        invoice_id=target_inv.id,
                        matched_santim=target_item.matched_santim,
                        tier=(res.tier.value if res.tier else MatchTier.TIER_B.value),
                        state=MatchState.PROPOSED.value,
                        created_at=now,
                    )
                    session.add(pm)
                    queued_proposed_matches.append((pl, pm))
                pl.state = PaymentState.PROPOSED.value

            else:
                pl.state = PaymentState.UNMATCHED.value
                unmatched_lines.append(pl)

        session.flush()

        # 5. Compute Metrics: Aging Summary
        aging_rep = generate_aging_report(session, tenant_id=temp_tenant.id, as_of_date=analysis_date)

        grand_remaining = aging_rep.grand_total_outstanding_santim
        grand_overdue = aging_rep.grand_total_overdue_santim

        bucket_items = [
            ("current", aging_rep.total_current_santim, "Current (Not Due)"),
            ("1_30", aging_rep.total_1_30_santim, "1–30 Days Overdue"),
            ("31_60", aging_rep.total_31_60_santim, "31–60 Days Overdue"),
            ("61_90", aging_rep.total_61_90_santim, "61–90 Days Overdue"),
            ("90_plus", aging_rep.total_90_plus_santim, "90+ Days Overdue"),
        ]

        # Calculate counts per bucket
        bucket_counts: dict[str, int] = {"current": 0, "1_30": 0, "31_60": 0, "61_90": 0, "90_plus": 0}
        for r_summary in aging_rep.retailers:
            for inv_item in r_summary.invoices:
                bucket_counts[inv_item.bucket] = bucket_counts.get(inv_item.bucket, 0) + 1

        rendered_buckets = []
        for b_key, b_amt, b_label in bucket_items:
            pct = (b_amt / grand_remaining * 100.0) if grand_remaining > 0 else 0.0
            rendered_buckets.append({
                "key": b_key,
                "label": b_label,
                "remaining_santim": b_amt,
                "remaining_etb": to_etb(b_amt),
                "count": bucket_counts.get(b_key, 0),
                "pct_debt": pct,
                "original_etb": to_etb(b_amt),  # Open balance is baseline for prospective health-check
                "paid_etb": "0.00 ETB",
            })

        # Top 10 Overdue Retailers
        sorted_retailers = sorted(
            aging_rep.retailers,
            key=lambda r: r.total_overdue_santim,
            reverse=True,
        )

        top_10_debtors = []
        # Calculate DSO per retailer: avg(payment.value_date - invoice.issued_date)
        retailer_days_to_collect: dict[uuid.UUID, list[int]] = {}
        for pl, pm in auto_confirmed_matches:
            inv = invoices_by_id.get(pm.invoice_id)
            if inv:
                d = (pl.value_date - inv.issued_date).days
                retailer_days_to_collect.setdefault(inv.retailer_id, []).append(max(0, d))

        for r in sorted_retailers[:10]:
            if r.total_overdue_santim <= 0 and r.total_outstanding_santim <= 0:
                continue
            dso_list = retailer_days_to_collect.get(r.retailer_id, [])
            avg_dso = sum(dso_list) / len(dso_list) if dso_list else None
            pct_overdue = (r.total_overdue_santim / grand_overdue * 100.0) if grand_overdue > 0 else 0.0
            top_10_debtors.append({
                "id": str(r.retailer_id),
                "name": r.retailer_name,
                "phone": r.phone,
                "overdue_santim": r.total_overdue_santim,
                "overdue_etb": to_etb(r.total_overdue_santim),
                "outstanding_santim": r.total_outstanding_santim,
                "outstanding_etb": to_etb(r.total_outstanding_santim),
                "avg_days_to_collect": avg_dso,
                "pct_of_overdue": pct_overdue,
            })

        # Matching Statistics
        auto_amt = sum(pm.matched_santim for _, pm in auto_confirmed_matches)
        queued_amt = sum(pm.matched_santim for _, pm in queued_proposed_matches)
        unmatched_amt = sum(pl.amount_santim for pl in unmatched_lines)
        total_stmt_lines = len(imported_payment_lines)
        total_stmt_santim = sum(pl.amount_santim for pl in imported_payment_lines)

        matching_summary = {
            "total_lines": total_stmt_lines,
            "total_amount_santim": total_stmt_santim,
            "total_amount_etb": to_etb(total_stmt_santim),
            "auto_confirmed": {
                "count": len(auto_confirmed_matches),
                "amount_santim": auto_amt,
                "amount_etb": to_etb(auto_amt),
                "notes": "Exact invoice token in narration with matching amount and verified bank proof.",
            },
            "queued": {
                "count": len(queued_proposed_matches),
                "amount_santim": queued_amt,
                "amount_etb": to_etb(queued_amt),
                "notes": "Proposed matches (counterparty phone, fuzzy merchant name, or partial payment) awaiting owner review.",
            },
            "unmatched": {
                "count": len(unmatched_lines),
                "amount_santim": unmatched_amt,
                "amount_etb": to_etb(unmatched_amt),
                "notes": "Inflows received into bank or Telebirr with no candidate invoice found.",
            },
        }

        # 6. Discrepancies: Overpayments, Duplicate Payments, Sequence Gaps, Unidentified Deposits
        duplicates_and_overpayments = []
        for pl, pm in queued_proposed_matches:
            inv = invoices_by_id.get(pm.invoice_id)
            if inv and pl.amount_santim > inv.amount_santim:
                excess = pl.amount_santim - inv.amount_santim
                duplicates_and_overpayments.append({
                    "invoice_number": inv.invoice_number,
                    "payment_ref": pl.raw_reference or pl.source_ref or "Bank Transfer",
                    "date": pl.value_date,
                    "amount_etb": to_etb(pl.amount_santim),
                    "excess_etb": to_etb(excess),
                    "reason": f"Payment exceeds invoice amount by {to_etb(excess)} (Unallocated Credit)",
                })

        # Statement line duplicates: identical (amount, ref, date) appearing multiple times
        seen_pl_keys: dict[tuple[int, str, date], list[PaymentLine]] = {}
        for pl in imported_payment_lines:
            key = (pl.amount_santim, (pl.raw_reference or "").strip(), pl.value_date)
            seen_pl_keys.setdefault(key, []).append(pl)

        for (amt, ref, v_date), pl_group in seen_pl_keys.items():
            if len(pl_group) > 1 and amt > 0:
                duplicates_and_overpayments.append({
                    "invoice_number": "Multiple Lines",
                    "payment_ref": ref or "Direct Deposit",
                    "date": v_date,
                    "amount_etb": to_etb(amt),
                    "excess_etb": to_etb(amt * (len(pl_group) - 1)),
                    "reason": f"Possible duplicate statement line: {len(pl_group)} identical payments of {to_etb(amt)} on {v_date}",
                })

        # Sequence Gaps
        sequence_gaps = detect_sequence_gaps(raw_invoice_numbers)

        # Unidentified deposits
        unidentified_deposits = [
            {
                "date": pl.value_date,
                "reference": pl.raw_reference or pl.source_ref or "Bank Inflow",
                "counterparty": pl.counterparty_name or pl.counterparty_account or "Unknown Payer",
                "amount_etb": to_etb(pl.amount_santim),
                "evidence_level": pl.evidence_level,
            }
            for pl in unmatched_lines
        ]

        # 7. 'Fix These First' Prioritized Action List
        action_items = []
        if duplicates_and_overpayments:
            dup_total = len(duplicates_and_overpayments)
            action_items.append({
                "title": f"Review {dup_total} Potential Duplicate / Overpaid Transactions",
                "desc": "Verify potential double-charges or overpayments to prevent customer balance disputes.",
            })

        if top_10_debtors and grand_overdue > 0:
            top_3 = top_10_debtors[:3]
            top_3_amt = sum(d["overdue_santim"] for d in top_3)
            top_3_pct = (top_3_amt / grand_overdue * 100.0)
            names_preview = ", ".join(d["name"] for d in top_3)
            action_items.append({
                "title": f"Target Top Overdue Debtors ({top_3_pct:.1f}% of Overdue Debt)",
                "desc": f"Focus collection on {names_preview} holding {to_etb(top_3_amt)} in overdue balances.",
            })

        oldest_debt = aging_rep.total_90_plus_santim
        if oldest_debt > 0:
            action_items.append({
                "title": f"Escalate 90+ Days Overdue Receivables ({to_etb(oldest_debt)})",
                "desc": "Oldest debt bucket carries severe default risk. Issue formal reminder letters or require cash-only terms.",
            })

        if unidentified_deposits:
            unmatched_cnt = len(unidentified_deposits)
            action_items.append({
                "title": f"Identify {unmatched_cnt} Unlinked Bank Deposits ({to_etb(unmatched_amt)})",
                "desc": "Check with bank or sales reps to associate unlinked deposits with customer orders or cash receipts.",
            })

        if sequence_gaps:
            action_items.append({
                "title": f"Audit {len(sequence_gaps)} Missing Invoice Sequence Numbers",
                "desc": f"Investigate physical receipt books for missing serial numbers: {', '.join(sequence_gaps[:6])}...",
            })

        if not action_items:
            action_items.append({
                "title": "Healthy Portfolio Detected",
                "desc": "All analyzed invoices and bank statement records are reconciled with no critical discrepancies found.",
            })

        # Assemble full report data
        report_data = {
            "meta": {
                "tenant_name": temp_tenant.name,
                "as_of_date": analysis_date,
                "total_invoices_count": len(created_invoices),
                "total_statement_lines_count": total_stmt_lines,
                "source_files": [invoices_p.name] + [p.name for p in stmt_paths],
            },
            "aging": {
                "buckets": rendered_buckets,
                "grand_total_count": sum(b["count"] for b in rendered_buckets),
                "grand_remaining_santim": grand_remaining,
                "grand_remaining_etb": to_etb(grand_remaining),
                "grand_original_etb": to_etb(grand_remaining),
                "grand_paid_etb": to_etb(auto_amt),
                "grand_overdue_santim": grand_overdue,
                "grand_overdue_etb": to_etb(grand_overdue),
            },
            "top_debtors": top_10_debtors,
            "matching": matching_summary,
            "discrepancies": {
                "duplicates_and_overpayments": duplicates_and_overpayments,
                "sequence_gaps": sequence_gaps,
                "unidentified_deposits": unidentified_deposits,
            },
            "action_items": action_items,
        }

        # Render HTML
        html_content = render_collections_health_check_html(report_data, lang=lang)
        out_p = Path(out_path)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        with open(out_p, "w", encoding="utf-8") as f:
            f.write(html_content)

        return report_data

    finally:
        if keep:
            session.commit()
            logger.info("Retained temporary health check tenant id=%s in database (--keep specified).", temp_tenant_id)
        else:
            session.rollback()
            logger.info("Cleaned up temporary health check tenant id=%s (rollback completed).", temp_tenant_id)
        session.close()
