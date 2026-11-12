"""
app/importers/wizard.py

Statement Import Wizard Engine.
Autodetects file encoding, delimiter, header row offset, number formats,
and date formats with a strict zero-guessing principle for ambiguous dates.
Provides 20-row preview, row-level error logging, and idempotent batch ingestion.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.importers.base import StatementImportError
from app.importers.mapping import (
    CBE_CSV_CONFIG,
    DASHEN_CSV_CONFIG,
    TELEBIRR_XLSX_CONFIG,
    ColumnMappingConfig,
)
from app.kernel.enums import EvidenceLevel, PaymentState
from app.kernel.events import append_event
from app.kernel.models import ImportBatch, ImportBatchError, PaymentLine, TenantImportMapping
from app.kernel.money import format_money, to_etb, validate_santim
from app.matching.normalization import string_similarity


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------

@dataclass
class DetectedFormat:
    encoding: str
    delimiter: str
    header_row_index: int
    headers: list[str]
    number_format: str  # "1,234.56" or "1.234,56"
    date_format: str | None
    is_date_ambiguous: bool
    ambiguous_sample: str | None
    suggested_source_name: str
    suggested_mapping: dict[str, str | None]


@dataclass
class PreviewRow:
    row_index: int
    raw_data: dict[str, str]
    raw_date: str | None = None
    parsed_date: str | None = None
    raw_amount: str | None = None
    parsed_santim: int | None = None
    parsed_etb: str | None = None
    counterparty: str | None = None
    reference: str | None = None
    is_credit: bool = True
    error: str | None = None


@dataclass
class WizardPreviewResult:
    detected_format: DetectedFormat
    preview_rows: list[PreviewRow]
    total_data_rows: int
    valid_credit_rows: int
    error_count: int
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Format Detection Functions
# ---------------------------------------------------------------------------

def detect_encoding(raw_bytes: bytes) -> str:
    """Detect encoding: UTF-8 with BOM, UTF-8, CP1252, or ISO-8859-1."""
    if raw_bytes.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    try:
        raw_bytes.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass

    try:
        raw_bytes.decode("cp1252")
        return "cp1252"
    except UnicodeDecodeError:
        pass

    return "iso-8859-1"


def decode_content(raw_bytes: bytes, encoding: str | None = None) -> str:
    """Decodes raw bytes using detected or specified encoding."""
    enc = encoding or detect_encoding(raw_bytes)
    return raw_bytes.decode(enc, errors="replace")


def detect_delimiter(lines: list[str]) -> str:
    """Detect delimiter among comma, semicolon, tab, pipe."""
    candidates = [",", ";", "\t", "|"]
    best_delim = ","
    max_score = -1

    sample_lines = [line.strip() for line in lines[:15] if line.strip()]
    if not sample_lines:
        return ","

    for d in candidates:
        counts = [len(line.split(d)) for line in sample_lines]
        if not counts:
            continue
        # Delimiter must split into at least 2 columns
        avg = sum(counts) / len(counts)
        if avg > 1.5:
            # Check consistency (variance)
            variance = sum((c - avg) ** 2 for c in counts) / len(counts)
            score = avg / (1.0 + variance)
            if score > max_score:
                max_score = score
                best_delim = d

    return best_delim


def detect_header_row(lines: list[str], delimiter: str, max_scan: int = 10) -> tuple[int, list[str]]:
    """
    Scans up to max_scan rows to find the most probable table header line.
    Skips metadata/title headers (e.g., bank letterheads, account numbers).
    """
    header_keywords = {
        "date", "posting date", "value date", "txn date", "transaction date",
        "amount", "credit", "debit", "credit amount", "debit amount", "net amount",
        "ref", "reference", "reference no", "narration", "description", "details",
        "counterparty", "counterparty name", "customer name", "sender", "payee",
        "account", "account number", "balance", "txn ref", "receipt no", "type"
    }

    best_idx = 0
    best_score = -1
    best_headers: list[str] = []

    for idx, raw_line in enumerate(lines[:max_scan]):
        line = raw_line.strip()
        if not line:
            continue
        reader = csv.reader([line], delimiter=delimiter)
        try:
            row = next(reader)
        except Exception:
            row = line.split(delimiter)

        cleaned_cols = [c.strip() for c in row if c.strip()]
        if len(cleaned_cols) < 2:
            continue

        score = 0
        for col in cleaned_cols:
            col_lower = col.lower()
            if any(kw in col_lower for kw in header_keywords):
                score += 2
            # Penalize purely numeric cells (these are data rows, not headers)
            clean_num = re.sub(r"[^\d]", "", col)
            if clean_num and len(clean_num) > 4 and col.replace(".", "").replace(",", "").replace("-", "").isdigit():
                score -= 3

        if score > best_score:
            best_score = score
            best_idx = idx
            best_headers = [c.strip() for c in row]

    if not best_headers and lines:
        reader = csv.reader([lines[0]], delimiter=delimiter)
        try:
            best_headers = [c.strip() for c in next(reader)]
        except Exception:
            best_headers = [c.strip() for c in lines[0].split(delimiter)]
        best_idx = 0

    return best_idx, best_headers


def detect_number_format(sample_values: list[str]) -> str:
    """
    Detect whether standard '1,234.56' or European '1.234,56' is used.
    """
    european_count = 0
    standard_count = 0

    for val in sample_values:
        v = str(val).strip()
        # Clean currency codes
        v = re.sub(r"[^\d.,-]", "", v)
        if not v:
            continue
        # European format: dot as thousands, comma as decimal (e.g. 1.250,50 or 250,50)
        if re.search(r",\d{2}$", v):
            european_count += 1
        # Standard format: comma as thousands, dot as decimal (e.g. 1,250.50 or 250.50)
        elif re.search(r"\.\d{2}$", v):
            standard_count += 1

    if european_count > standard_count:
        return "1.234,56"
    return "1,234.56"


def parse_wizard_amount(raw_val: Any, number_format: str = "1,234.56") -> int:
    """
    Parses an amount into integer santim using the specified number format.
    Zero-guessing: never uses float.
    """
    if raw_val is None:
        raise StatementImportError("Empty amount")

    val_str = str(raw_val).strip()
    if not val_str or val_str in ("-", "--", "N/A"):
        raise StatementImportError("Empty or null amount")

    # Clean whitespace and currency prefixes/suffixes
    val_str = re.sub(r"[^\d.,-]", "", val_str)
    if not val_str:
        raise StatementImportError(f"Invalid amount value: {raw_val!r}")

    is_negative = val_str.startswith("-")
    if is_negative:
        val_str = val_str[1:]

    if number_format == "1.234,56":
        # European: strip dots, replace comma with dot
        val_str = val_str.replace(".", "")
        val_str = val_str.replace(",", ".")
    else:
        # Standard: strip commas
        val_str = val_str.replace(",", "")

    if "." in val_str:
        parts = val_str.split(".", 1)
        birr_part = parts[0] or "0"
        cents_part = parts[1]
        if len(cents_part) > 2:
            cents_part = cents_part[:2]
        else:
            cents_part = cents_part.ljust(2, "0")
    else:
        birr_part = val_str
        cents_part = "00"

    try:
        birr = int(birr_part)
        cents = int(cents_part)
        santim = birr * 100 + cents
        if is_negative:
            santim = -santim
    except ValueError as exc:
        raise StatementImportError(f"Could not parse amount: {raw_val!r}") from exc

    return santim


def detect_date_format(sample_dates: list[str]) -> tuple[str | None, bool, str | None]:
    """
    Detects date format and flags whether dates are ambiguous.
    Returns: (detected_format, is_ambiguous, ambiguous_sample)
    Strict Zero-Guessing: If all sample dates have day <= 12 and month <= 12,
    ambiguity is flagged and user selection is required.
    """
    clean_samples = [str(d).strip() for d in sample_dates if str(d).strip()]
    if not clean_samples:
        return ("%Y-%m-%d", False, None)

    # Check ISO format YYYY-MM-DD
    all_iso = all(re.match(r"^\d{4}-\d{2}-\d{2}", s) for s in clean_samples)
    if all_iso:
        return ("%Y-%m-%d", False, None)

    # Check DD-b-YYYY (e.g. 24-Oct-2026)
    all_month_name = all(re.match(r"^\d{1,2}-[A-Za-z]{3}-\d{4}", s) for s in clean_samples)
    if all_month_name:
        return ("%d-%b-%Y", False, None)

    # Check slash dates: A/B/YYYY
    slash_dates = [s for s in clean_samples if re.match(r"^\d{1,2}/\d{1,2}/\d{4}", s)]
    if slash_dates:
        can_be_dmy = True
        can_be_mdy = True
        has_day_gt_12 = False
        has_month_gt_12 = False

        for s in slash_dates:
            parts = re.split(r"[/ ]", s)
            p1 = int(parts[0])
            p2 = int(parts[1])

            if p1 > 12:
                has_day_gt_12 = True
                can_be_mdy = False
            if p2 > 12:
                has_month_gt_12 = True
                can_be_dmy = False

        if has_day_gt_12:
            return ("%d/%m/%Y", False, None)
        if has_month_gt_12:
            return ("%m/%d/%Y", False, None)
        if can_be_dmy and can_be_mdy:
            # Ambiguous! Both parts <= 12 in all samples
            return ("%d/%m/%Y", True, slash_dates[0])

    # Check dash dates: A-B-YYYY
    dash_dates = [s for s in clean_samples if re.match(r"^\d{1,2}-\d{1,2}-\d{4}", s)]
    if dash_dates:
        can_be_dmy = True
        can_be_mdy = True
        has_p1_gt_12 = False
        has_p2_gt_12 = False

        for s in dash_dates:
            parts = re.split(r"[- ]", s)
            p1 = int(parts[0])
            p2 = int(parts[1])
            if p1 > 12:
                has_p1_gt_12 = True
                can_be_mdy = False
            if p2 > 12:
                has_p2_gt_12 = True
                can_be_dmy = False

        if has_p1_gt_12:
            return ("%d-%m-%Y", False, None)
        if has_p2_gt_12:
            return ("%m-%d-%Y", False, None)
        if can_be_dmy and can_be_mdy:
            return ("%d-%m-%Y", True, dash_dates[0])

    # Check dot dates: A.B.YYYY
    dot_dates = [s for s in clean_samples if re.match(r"^\d{1,2}\.\d{1,2}\.\d{4}", s)]
    if dot_dates:
        for s in dot_dates:
            parts = re.split(r"[\. ]", s)
            if int(parts[0]) > 12:
                return ("%d.%m.%Y", False, None)
        return ("%d.%m.%Y", True, dot_dates[0])

    return ("%Y-%m-%d", False, None)


def parse_wizard_date(date_str: Any, fmt: str) -> date:
    """Parses date string strictly using given format."""
    if not date_str:
        raise StatementImportError("Empty date")
    clean_str = str(date_str).strip()
    # Strip any time component if format does not have %H
    if "%H" not in fmt and (" " in clean_str or "T" in clean_str):
        clean_str = re.split(r"[ T]", clean_str)[0]

    try:
        return datetime.strptime(clean_str, fmt).date()
    except ValueError as exc:
        raise StatementImportError(f"Date '{clean_str}' does not match format '{fmt}'") from exc


def suggest_column_mapping(headers: list[str]) -> tuple[dict[str, str | None], str]:
    """
    Scores headers against standard fields and Ethiopian built-in configs.
    Returns (mapping_dict, suggested_source_name).
    """
    mapping: dict[str, str | None] = {
        "date_column": None,
        "amount_column": None,
        "credit_column": None,
        "debit_column": None,
        "reference_column": None,
        "narrative_column": None,
        "counterparty_name_column": None,
        "counterparty_account_column": None,
        "tx_id_column": None,
    }

    # Normalize header lookup
    norm_headers = {h.lower().strip(): h for h in headers if h.strip()}

    for norm_k, orig_h in norm_headers.items():
        # Date
        if not mapping["date_column"] and any(w in norm_k for w in ["posting date", "value date", "trans date", "transaction date", "date"]):
            mapping["date_column"] = orig_h

        # Credit / Debit split
        if "credit" in norm_k and not mapping["credit_column"]:
            mapping["credit_column"] = orig_h
        elif "debit" in norm_k and not mapping["debit_column"]:
            mapping["debit_column"] = orig_h
        elif any(w in norm_k for w in ["amount", "net amount", "trans amount"]) and not mapping["amount_column"]:
            mapping["amount_column"] = orig_h

        # Reference
        if any(w in norm_k for w in ["ref", "reference", "remarks", "cheque", "check"]):
            if not mapping["reference_column"]:
                mapping["reference_column"] = orig_h

        # Narrative / Description
        if any(w in norm_k for w in ["description", "narration", "particulars", "details", "service name"]):
            if not mapping["narrative_column"]:
                mapping["narrative_column"] = orig_h

        # Counterparty Name
        if any(w in norm_k for w in ["counterparty name", "customer name", "sender name", "party name", "beneficiary"]):
            if not mapping["counterparty_name_column"]:
                mapping["counterparty_name_column"] = orig_h

        # Counterparty Account
        if any(w in norm_k for w in ["counterparty account", "source account", "sender mobile", "account no", "acc no"]):
            if not mapping["counterparty_account_column"]:
                mapping["counterparty_account_column"] = orig_h

        # Transaction ID
        if any(w in norm_k for w in ["transaction id", "receipt no", "txn ref", "tx id", "ft ref", "reference id"]):
            if not mapping["tx_id_column"]:
                mapping["tx_id_column"] = orig_h

    # Fallback for amount if credit_column detected but no general amount
    if mapping["credit_column"] and not mapping["amount_column"]:
        mapping["amount_column"] = mapping["credit_column"]

    # Match against standard configs
    suggested_source = "custom_statement"
    header_set = set(headers)
    if "Posting Date" in header_set and "Credit Amount" in header_set:
        suggested_source = "cbe_csv"
    elif "Value Date" in header_set and "Credit" in header_set:
        suggested_source = "dashen_csv"
    elif "Transaction Time" in header_set and "Net Amount" in header_set:
        suggested_source = "telebirr_xlsx"

    return mapping, suggested_source


# ---------------------------------------------------------------------------
# Wizard Analysis and Preview Generator
# ---------------------------------------------------------------------------

def analyze_statement_file(raw_bytes: bytes, filename: str | None = None) -> DetectedFormat:
    """Performs full autodetection on an uploaded CSV statement file."""
    encoding = detect_encoding(raw_bytes)
    text = decode_content(raw_bytes, encoding)
    lines = [line.rstrip("\r\n") for line in text.splitlines() if line.strip()]

    if not lines:
        raise StatementImportError("Uploaded statement file is empty.")

    delimiter = detect_delimiter(lines)
    header_idx, headers = detect_header_row(lines, delimiter)

    data_lines = lines[header_idx + 1:]
    reader = csv.reader(data_lines[:30], delimiter=delimiter)
    sample_rows = list(reader)

    # Collect sample dates and amounts from likely columns
    sample_dates: list[str] = []
    sample_amounts: list[str] = []

    date_col_idx = None
    amount_col_idx = None

    for col_i, h in enumerate(headers):
        hl = h.lower()
        if not date_col_idx and any(w in hl for w in ["date", "time"]):
            date_col_idx = col_i
        if not amount_col_idx and any(w in hl for w in ["amount", "credit"]):
            amount_col_idx = col_i

    for row in sample_rows:
        if date_col_idx is not None and date_col_idx < len(row):
            sample_dates.append(row[date_col_idx])
        if amount_col_idx is not None and amount_col_idx < len(row):
            sample_amounts.append(row[amount_col_idx])

    number_format = detect_number_format(sample_amounts)
    date_fmt, is_ambiguous, ambiguous_sample = detect_date_format(sample_dates)
    mapping, suggested_source = suggest_column_mapping(headers)

    return DetectedFormat(
        encoding=encoding,
        delimiter=delimiter,
        header_row_index=header_idx,
        headers=headers,
        number_format=number_format,
        date_format=date_fmt,
        is_date_ambiguous=is_ambiguous,
        ambiguous_sample=ambiguous_sample,
        suggested_source_name=suggested_source,
        suggested_mapping=mapping,
    )


def generate_preview(
    raw_bytes: bytes,
    fmt: DetectedFormat,
    custom_mapping: dict[str, str | None] | None = None,
    override_date_format: str | None = None,
    max_preview_rows: int = 20,
) -> WizardPreviewResult:
    """
    Parses and builds the 20-row preview, side-by-side raw vs parsed values,
    flagging errors and ambiguous dates.
    """
    encoding = fmt.encoding
    text = decode_content(raw_bytes, encoding)
    lines = [line.rstrip("\r\n") for line in text.splitlines() if line.strip()]

    data_lines = lines[fmt.header_row_index + 1:]
    reader = csv.reader(data_lines, delimiter=fmt.delimiter)

    mapping = custom_mapping or fmt.suggested_mapping
    headers = fmt.headers

    header_to_idx = {h: idx for idx, h in enumerate(headers)}

    date_col = mapping.get("date_column")
    credit_col = mapping.get("credit_column")
    debit_col = mapping.get("debit_column")
    amount_col = mapping.get("amount_column") or credit_col
    ref_col = mapping.get("reference_column")
    narr_col = mapping.get("narrative_column")
    cp_name_col = mapping.get("counterparty_name_column")
    tx_id_col = mapping.get("tx_id_column")

    date_fmt = override_date_format or fmt.date_format or "%Y-%m-%d"
    is_ambiguous = fmt.is_date_ambiguous and not override_date_format

    preview_rows: list[PreviewRow] = []
    total_data_rows = 0
    valid_credit_rows = 0
    error_count = 0
    warnings: list[str] = []

    if is_ambiguous:
        warnings.append(
            f"Date format is ambiguous for sample date '{fmt.ambiguous_sample}'. "
            "Please select Day/Month or Month/Day explicitly before importing."
        )

    for row_idx, row in enumerate(reader, start=1):
        total_data_rows += 1
        raw_dict = {}
        for h_idx, h in enumerate(headers):
            val = row[h_idx] if h_idx < len(row) else ""
            raw_dict[h] = val

        raw_date = raw_dict.get(date_col) if date_col else None
        raw_amount = None

        # Polarity / Credit-Debit column resolution
        is_credit = True
        if credit_col and raw_dict.get(credit_col):
            raw_amount = raw_dict[credit_col]
        elif debit_col and raw_dict.get(debit_col):
            raw_amount = raw_dict[debit_col]
            is_credit = False  # Explicit debit column entry
        elif amount_col:
            raw_amount = raw_dict.get(amount_col)

        row_error: str | None = None
        parsed_dt: str | None = None
        parsed_santim: int | None = None
        parsed_etb: str | None = None

        # Parse Date
        if is_ambiguous:
            row_error = f"Ambiguous date format '{raw_date}' requires explicit selection"
        elif raw_date:
            try:
                dt = parse_wizard_date(raw_date, date_fmt)
                parsed_dt = dt.isoformat()
            except StatementImportError as e:
                row_error = str(e)

        # Parse Amount
        if raw_amount and not row_error:
            try:
                amt = parse_wizard_amount(raw_amount, fmt.number_format)
                if amt <= 0:
                    is_credit = False
                else:
                    parsed_santim = amt
                    parsed_etb = f"{to_etb(amt)} ETB"
            except StatementImportError as e:
                row_error = str(e)

        if row_error:
            error_count += 1
        elif is_credit and parsed_santim and parsed_santim > 0:
            valid_credit_rows += 1

        # Populate preview for first N rows
        if len(preview_rows) < max_preview_rows:
            cp_val = raw_dict.get(cp_name_col) if cp_name_col else None
            ref_parts = []
            if ref_col and raw_dict.get(ref_col):
                ref_parts.append(raw_dict[ref_col])
            if narr_col and raw_dict.get(narr_col):
                ref_parts.append(raw_dict[narr_col])
            ref_val = " | ".join(ref_parts) if ref_parts else None

            preview_rows.append(
                PreviewRow(
                    row_index=row_idx,
                    raw_data=raw_dict,
                    raw_date=raw_date,
                    parsed_date=parsed_dt,
                    raw_amount=raw_amount,
                    parsed_santim=parsed_santim,
                    parsed_etb=parsed_etb,
                    counterparty=cp_val,
                    reference=ref_val,
                    is_credit=is_credit,
                    error=row_error,
                )
            )

    return WizardPreviewResult(
        detected_format=fmt,
        preview_rows=preview_rows,
        total_data_rows=total_data_rows,
        valid_credit_rows=valid_credit_rows,
        error_count=error_count,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Wizard Statement Ingestion
# ---------------------------------------------------------------------------

def execute_wizard_import(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    raw_bytes: bytes,
    filename: str,
    detected_format: DetectedFormat,
    custom_mapping: dict[str, str | None],
    date_format: str,
    number_format: str = "1,234.56",
    save_mapping_as: str | None = None,
) -> tuple[ImportBatch, list[PaymentLine], list[ImportBatchError]]:
    """
    Ingests statement rows idempotently according to mapping.
    Records ImportBatch, PaymentLines, ImportBatchError rows, and append-only event.
    """
    file_hash = hashlib.sha256(raw_bytes).hexdigest()

    # 1. Idempotency Check
    existing_batch = session.execute(
        select(ImportBatch).where(
            ImportBatch.tenant_id == tenant_id,
            ImportBatch.file_hash == file_hash,
        )
    ).scalar_one_or_none()

    if existing_batch:
        existing_lines = session.execute(
            select(PaymentLine).where(PaymentLine.import_batch_id == existing_batch.id)
        ).scalars().all()
        existing_errors = session.execute(
            select(ImportBatchError).where(ImportBatchError.import_batch_id == existing_batch.id)
        ).scalars().all()
        return existing_batch, list(existing_lines), list(existing_errors)

    # 2. Optionally Save Mapping Config
    if save_mapping_as:
        existing_mapping = session.execute(
            select(TenantImportMapping).where(
                TenantImportMapping.tenant_id == tenant_id,
                TenantImportMapping.source_name == save_mapping_as,
            )
        ).scalar_one_or_none()

        if existing_mapping:
            existing_mapping.delimiter = detected_format.delimiter
            existing_mapping.encoding = detected_format.encoding
            existing_mapping.header_row_index = detected_format.header_row_index
            existing_mapping.date_column = custom_mapping.get("date_column") or "Date"
            existing_mapping.date_format = date_format
            existing_mapping.amount_column = custom_mapping.get("amount_column")
            existing_mapping.credit_column = custom_mapping.get("credit_column")
            existing_mapping.debit_column = custom_mapping.get("debit_column")
            existing_mapping.reference_column = custom_mapping.get("reference_column")
            existing_mapping.narrative_column = custom_mapping.get("narrative_column")
            existing_mapping.counterparty_name_column = custom_mapping.get("counterparty_name_column")
            existing_mapping.counterparty_account_column = custom_mapping.get("counterparty_account_column")
            existing_mapping.tx_id_column = custom_mapping.get("tx_id_column")
            existing_mapping.number_format = number_format
            existing_mapping.updated_at = datetime.now(timezone.utc)
        else:
            new_map = TenantImportMapping(
                tenant_id=tenant_id,
                source_name=save_mapping_as,
                delimiter=detected_format.delimiter,
                encoding=detected_format.encoding,
                header_row_index=detected_format.header_row_index,
                date_column=custom_mapping.get("date_column") or "Date",
                date_format=date_format,
                amount_column=custom_mapping.get("amount_column"),
                credit_column=custom_mapping.get("credit_column"),
                debit_column=custom_mapping.get("debit_column"),
                reference_column=custom_mapping.get("reference_column"),
                narrative_column=custom_mapping.get("narrative_column"),
                counterparty_name_column=custom_mapping.get("counterparty_name_column"),
                counterparty_account_column=custom_mapping.get("counterparty_account_column"),
                tx_id_column=custom_mapping.get("tx_id_column"),
                number_format=number_format,
                created_by=user_id,
            )
            session.add(new_map)

    # 3. Create ImportBatch
    batch_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    source_name = save_mapping_as or detected_format.suggested_source_name

    batch = ImportBatch(
        id=batch_id,
        tenant_id=tenant_id,
        source_type=source_name,
        evidence_level=EvidenceLevel.L2.value,
        filename=filename,
        file_hash=file_hash,
        imported_by=user_id,
        imported_at=now,
    )
    session.add(batch)

    # 4. Ingest and Validate Rows
    text = decode_content(raw_bytes, detected_format.encoding)
    lines = [line.rstrip("\r\n") for line in text.splitlines() if line.strip()]
    data_lines = lines[detected_format.header_row_index + 1:]
    reader = csv.reader(data_lines, delimiter=detected_format.delimiter)

    headers = detected_format.headers
    date_col = custom_mapping.get("date_column")
    credit_col = custom_mapping.get("credit_column")
    debit_col = custom_mapping.get("debit_column")
    amount_col = custom_mapping.get("amount_column") or credit_col
    ref_col = custom_mapping.get("reference_column")
    narr_col = custom_mapping.get("narrative_column")
    cp_name_col = custom_mapping.get("counterparty_name_column")
    cp_acc_col = custom_mapping.get("counterparty_account_column")
    tx_id_col = custom_mapping.get("tx_id_column")

    payment_lines: list[PaymentLine] = []
    error_records: list[ImportBatchError] = []

    for row_idx, row in enumerate(reader, start=1):
        raw_dict = {}
        for h_idx, h in enumerate(headers):
            val = row[h_idx] if h_idx < len(row) else ""
            raw_dict[h] = val

        raw_str = detected_format.delimiter.join(row)

        # Skip explicit debit column rows
        if debit_col and raw_dict.get(debit_col) and not (credit_col and raw_dict.get(credit_col)):
            continue

        raw_amount = raw_dict.get(credit_col) if credit_col and raw_dict.get(credit_col) else raw_dict.get(amount_col)
        if not raw_amount or str(raw_amount).strip() in ("", "0", "0.00", "-"):
            continue

        # Amount validation
        try:
            santim = parse_wizard_amount(raw_amount, number_format)
            if santim <= 0:
                # Debit / negative amount skipped
                continue
        except StatementImportError as err:
            error_records.append(
                ImportBatchError(
                    tenant_id=tenant_id,
                    import_batch_id=batch_id,
                    row_index=row_idx,
                    raw_data=raw_str,
                    error_reason=f"Amount error: {err}",
                )
            )
            continue

        # Date validation
        raw_date = raw_dict.get(date_col)
        try:
            val_date = parse_wizard_date(raw_date, date_format)
        except StatementImportError as err:
            error_records.append(
                ImportBatchError(
                    tenant_id=tenant_id,
                    import_batch_id=batch_id,
                    row_index=row_idx,
                    raw_data=raw_str,
                    error_reason=f"Date error: {err}",
                )
            )
            continue

        # Extract references
        ref_parts = []
        if ref_col and raw_dict.get(ref_col):
            ref_parts.append(str(raw_dict[ref_col]).strip())
        if narr_col and raw_dict.get(narr_col):
            ref_parts.append(str(raw_dict[narr_col]).strip())
        raw_ref = " | ".join(ref_parts) if ref_parts else None

        cp_name = str(raw_dict[cp_name_col]).strip() if cp_name_col and raw_dict.get(cp_name_col) else None
        cp_acc = str(raw_dict[cp_acc_col]).strip() if cp_acc_col and raw_dict.get(cp_acc_col) else None
        tx_id = str(raw_dict[tx_id_col]).strip() if tx_id_col and raw_dict.get(tx_id_col) else None

        short_hash = file_hash[:16]
        row_key = f"{source_name}:tx:{tx_id}" if tx_id else f"{source_name}:pos:{short_hash}:{row_idx}:{val_date}:{santim}"

        line = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            source_ref=tx_id,
            row_key=row_key,
            amount_santim=santim,
            value_date=val_date,
            counterparty_name=cp_name,
            counterparty_account=cp_acc,
            raw_reference=raw_ref,
            evidence_level=EvidenceLevel.L2.value,
            state=PaymentState.UNMATCHED.value,
            import_batch_id=batch_id,
            created_at=datetime.now(timezone.utc),
        )
        payment_lines.append(line)

    # Dedup and add lines
    for pl in payment_lines:
        if pl.row_key:
            exists = session.execute(
                select(PaymentLine.id).where(
                    PaymentLine.tenant_id == tenant_id,
                    PaymentLine.row_key == pl.row_key,
                )
            ).scalar_one_or_none()
            if exists:
                continue
        session.add(pl)

    for err in error_records:
        session.add(err)

    session.flush()

    # Append audit event
    append_event(
        session,
        tenant_id=tenant_id,
        event_type="statement.imported",
        aggregate_type="import_batch",
        aggregate_id=batch_id,
        actor_id=user_id,
        evidence_level=EvidenceLevel.L2,
        payload={
            "source_type": source_name,
            "filename": filename,
            "file_hash": file_hash,
            "lines_count": len(payment_lines),
            "errors_count": len(error_records),
            "total_santim": sum(l.amount_santim for l in payment_lines),
        },
    )

    return batch, payment_lines, error_records
