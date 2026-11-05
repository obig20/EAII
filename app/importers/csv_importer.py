"""
app/importers/csv_importer.py

CSV statement importer with delimiter auto-detection and UTF-8-sig support.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any

from app.importers.base import BaseImporter
from app.importers.mapping import ColumnMappingConfig


class CsvStatementImporter(BaseImporter):
    """Importer for CSV bank/mobile money exports."""

    def extract_raw_rows(self, content_or_path: Any) -> list[dict[str, Any]]:
        text_data: str

        if isinstance(content_or_path, (str, Path)) and Path(str(content_or_path)).is_file():
            with open(content_or_path, "r", encoding="utf-8-sig", errors="replace") as f:
                text_data = f.read()
        elif isinstance(content_or_path, bytes):
            text_data = content_or_path.decode("utf-8-sig", errors="replace")
        elif isinstance(content_or_path, str):
            text_data = content_or_path
        else:
            raise TypeError(f"Unsupported CSV input type: {type(content_or_path).__name__}")

        if not text_data.strip():
            return []

        # Sniff delimiter (comma, semicolon, tab, pipe)
        sample = text_data[:2048]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            delimiter = dialect.delimiter
        except Exception:
            delimiter = ","

        reader = csv.DictReader(io.StringIO(text_data), delimiter=delimiter)
        rows: list[dict[str, Any]] = []
        for r in reader:
            # Strip keys and values
            clean_row = {
                (k.strip() if k else ""): (v.strip() if isinstance(v, str) else v)
                for k, v in r.items()
                if k is not None
            }
            rows.append(clean_row)

        return rows
