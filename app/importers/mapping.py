"""
app/importers/mapping.py

Configurable column mapping schemas for bank and mobile money statements.
Allows any Ethiopian bank (CBE, Dashen, Awash, BOA) or Telebirr export
to be ingested without code changes.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.kernel.enums import EvidenceLevel


@dataclass
class ColumnMappingConfig:
    source_name: str
    evidence_level: EvidenceLevel = EvidenceLevel.L2
    date_column: str = "Date"
    date_format: str = "%Y-%m-%d"  # or "%d/%m/%Y", "%d-%b-%Y", etc.
    amount_column: str = "Amount"
    # Polarity detection: if statement has separate Debit/Credit columns or a Type column
    polarity_column: str | None = None
    credit_value: str | None = None  # e.g. "CR", "Credit", "In", "Payment"
    reference_column: str | None = None
    narrative_column: str | None = None
    counterparty_name_column: str | None = None
    counterparty_account_column: str | None = None
    tx_id_column: str | None = None
    skip_rows: int = 0
    # Additional static metadata
    extra_options: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ColumnMappingConfig:
        level_raw = data.get("evidence_level", "L2")
        evidence_level = EvidenceLevel(level_raw) if isinstance(level_raw, str) else level_raw
        return cls(
            source_name=data["source_name"],
            evidence_level=evidence_level,
            date_column=data.get("date_column", "Date"),
            date_format=data.get("date_format", "%Y-%m-%d"),
            amount_column=data.get("amount_column", "Amount"),
            polarity_column=data.get("polarity_column"),
            credit_value=data.get("credit_value"),
            reference_column=data.get("reference_column"),
            narrative_column=data.get("narrative_column"),
            counterparty_name_column=data.get("counterparty_name_column"),
            counterparty_account_column=data.get("counterparty_account_column"),
            tx_id_column=data.get("tx_id_column"),
            skip_rows=int(data.get("skip_rows", 0)),
            extra_options=data.get("extra_options", {}),
        )

    @classmethod
    def from_json_file(cls, path: str | Path) -> ColumnMappingConfig:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["evidence_level"] = self.evidence_level.value
        return d


# ---------------------------------------------------------------------------
# Default configurations for major Ethiopian sources
# ---------------------------------------------------------------------------

CBE_CSV_CONFIG = ColumnMappingConfig(
    source_name="cbe_csv",
    evidence_level=EvidenceLevel.L2,
    date_column="Posting Date",
    date_format="%d/%m/%Y",
    amount_column="Credit Amount",
    reference_column="Reference No",
    narrative_column="Description",
    counterparty_name_column="Counterparty Name",
    counterparty_account_column="Counterparty Account",
    tx_id_column="Transaction ID",
    skip_rows=0,
)

TELEBIRR_XLSX_CONFIG = ColumnMappingConfig(
    source_name="telebirr_xlsx",
    evidence_level=EvidenceLevel.L2,
    date_column="Transaction Time",
    date_format="%Y-%m-%d %H:%M:%S",
    amount_column="Net Amount",
    polarity_column="Type",
    credit_value="Payment Received",
    reference_column="Remarks",
    narrative_column="Service Name",
    counterparty_name_column="Sender Name",
    counterparty_account_column="Sender Mobile",
    tx_id_column="Receipt No",
    skip_rows=0,
)

DASHEN_CSV_CONFIG = ColumnMappingConfig(
    source_name="dashen_csv",
    evidence_level=EvidenceLevel.L2,
    date_column="Value Date",
    date_format="%d-%b-%Y",
    amount_column="Credit",
    reference_column="Narration",
    narrative_column="Transaction Details",
    counterparty_name_column="Customer Name",
    counterparty_account_column="Source Account",
    tx_id_column="Txn Ref",
    skip_rows=1,
)
