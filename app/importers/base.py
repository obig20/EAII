"""
app/importers/base.py

Base statement importer logic.
Converts raw row dicts into PaymentLine domain entities while strictly
enforcing Rule 1 (integer Santim) and Rule 4 (append-only event logging).
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timezone
from typing import Any, Sequence

from sqlalchemy.orm import Session

from app.importers.mapping import ColumnMappingConfig
from app.kernel.enums import EvidenceLevel, PaymentState
from app.kernel.events import append_event
from app.kernel.models import ImportBatch, PaymentLine
from app.kernel.money import validate_santim


class StatementImportError(ValueError):
    """Raised when statement format or values cannot be processed."""


def parse_amount_santim(raw_val: Any) -> int:
    """
    Parse a statement amount string/number into integer santim (Rule 1).
    Never uses float operations that could introduce binary rounding errors.
    """
    if raw_val is None:
        raise StatementImportError("Empty amount field")

    if isinstance(raw_val, int):
        # Already an integer: check if passed as raw birr or string
        return raw_val * 100

    val_str = str(raw_val).strip()
    if not val_str:
        raise StatementImportError("Empty amount string")

    # Clean currency symbols, commas, spaces
    val_str = re.sub(r"[^\d.-]", "", val_str)
    if not val_str:
        raise StatementImportError(f"Invalid amount value: {raw_val!r}")

    if "." in val_str:
        parts = val_str.split(".", 1)
        birr_part = parts[0] or "0"
        cents_part = parts[1]
        if len(cents_part) > 2:
            # Bank statement has >2 decimal places - truncate or reject
            cents_part = cents_part[:2]
        else:
            cents_part = cents_part.ljust(2, "0")
    else:
        birr_part = val_str
        cents_part = "00"

    try:
        sign = -1 if birr_part.startswith("-") else 1
        birr = abs(int(birr_part))
        cents = int(cents_part)
        santim = sign * (birr * 100 + cents)
    except ValueError as exc:
        raise StatementImportError(f"Could not parse amount: {raw_val!r}") from exc

    return validate_santim(santim)


def parse_statement_date(date_str: Any, fmt: str = "%Y-%m-%d") -> date:
    """Parse date from statement row with format fallbacks."""
    if isinstance(date_str, (datetime, date)):
        return date_str.date() if isinstance(date_str, datetime) else date_str

    if not date_str:
        return datetime.now(timezone.utc).date()

    clean_str = str(date_str).strip()

    # Try configured format
    formats_to_try = [
        fmt,
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%m/%d/%Y",
        "%d-%m-%Y",
        "%d-%b-%Y",
        "%Y-%m-%d %H:%M:%S",
        "%d/%m/%Y %H:%M:%S",
    ]

    for f in formats_to_try:
        try:
            return datetime.strptime(clean_str, f).date()
        except ValueError:
            continue

    # Fallback regex search for YYYY-MM-DD or DD/MM/YYYY
    iso_match = re.search(r"(\d{4})-(\d{2})-(\d{2})", clean_str)
    if iso_match:
        y, m, d = map(int, iso_match.groups())
        return date(y, m, d)

    dmy_match = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", clean_str)
    if dmy_match:
        d, m, y = map(int, dmy_match.groups())
        return date(y, m, d)

    raise StatementImportError(f"Unable to parse date string: {clean_str!r}")


class BaseImporter:
    """Base class for parsing tabular statements into PaymentLines."""

    def __init__(self, config: ColumnMappingConfig):
        self.config = config

    def extract_raw_rows(self, content_or_path: Any) -> list[dict[str, Any]]:
        raise NotImplementedError

    def parse_rows(
        self,
        raw_rows: Sequence[dict[str, Any]],
        *,
        tenant_id: uuid.UUID,
        batch_id: uuid.UUID,
    ) -> list[PaymentLine]:
        """Convert a sequence of raw row dicts into PaymentLine entities."""
        cfg = self.config
        lines: list[PaymentLine] = []

        for row in raw_rows[cfg.skip_rows:]:
            # Check polarity if configured (ignore debits/withdrawals)
            if cfg.polarity_column and cfg.credit_value:
                polarity_val = str(row.get(cfg.polarity_column, "")).strip().lower()
                expected = cfg.credit_value.strip().lower()
                if polarity_val != expected:
                    continue

            # Amount
            raw_amount = row.get(cfg.amount_column)
            if raw_amount is None or str(raw_amount).strip() in ("", "0", "0.00", "-"):
                continue

            try:
                santim = parse_amount_santim(raw_amount)
            except (StatementImportError, ValueError):
                continue  # Skip unparseable or negative rows

            # Date
            raw_date = row.get(cfg.date_column)
            val_date = parse_statement_date(raw_date, cfg.date_format)

            # References & narratives
            ref_parts = []
            if cfg.reference_column and row.get(cfg.reference_column):
                ref_parts.append(str(row[cfg.reference_column]).strip())
            if cfg.narrative_column and row.get(cfg.narrative_column):
                ref_parts.append(str(row[cfg.narrative_column]).strip())
            raw_ref = " | ".join(ref_parts) if ref_parts else None

            # Counterparty
            cp_name = (
                str(row[cfg.counterparty_name_column]).strip()
                if cfg.counterparty_name_column and row.get(cfg.counterparty_name_column)
                else None
            )
            cp_account = (
                str(row[cfg.counterparty_account_column]).strip()
                if cfg.counterparty_account_column and row.get(cfg.counterparty_account_column)
                else None
            )

            # Transaction ID
            tx_id = (
                str(row[cfg.tx_id_column]).strip()
                if cfg.tx_id_column and row.get(cfg.tx_id_column)
                else None
            )

            line = PaymentLine(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                source_ref=tx_id,
                amount_santim=santim,
                value_date=val_date,
                counterparty_name=cp_name,
                counterparty_account=cp_account,
                raw_reference=raw_ref,
                evidence_level=cfg.evidence_level.value,
                state=PaymentState.UNMATCHED.value,
                import_batch_id=batch_id,
                created_at=datetime.now(timezone.utc),
            )
            lines.append(line)

        return lines

    def import_statement(
        self,
        session: Session,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        content_or_path: Any,
        filename: str | None = None,
    ) -> tuple[ImportBatch, list[PaymentLine]]:
        """
        Parses statement, persists ImportBatch and PaymentLines,
        and records an append-only event log entry.
        """
        batch_id = uuid.uuid4()
        now = datetime.now(timezone.utc)

        batch = ImportBatch(
            id=batch_id,
            tenant_id=tenant_id,
            source_type=self.config.source_name,
            evidence_level=self.config.evidence_level.value,
            filename=filename,
            imported_by=user_id,
            imported_at=now,
        )
        session.add(batch)

        raw_rows = self.extract_raw_rows(content_or_path)
        lines = self.parse_rows(raw_rows, tenant_id=tenant_id, batch_id=batch_id)

        for line in lines:
            session.add(line)

        session.flush()

        # Append-only event for auditability (Rule 4)
        append_event(
            session,
            tenant_id=tenant_id,
            event_type="statement.imported",
            aggregate_type="import_batch",
            aggregate_id=batch_id,
            actor_id=user_id,
            evidence_level=self.config.evidence_level,
            payload={
                "source_type": self.config.source_name,
                "filename": filename,
                "lines_count": len(lines),
                "total_santim": sum(line.amount_santim for line in lines),
            },
        )

        return batch, lines
