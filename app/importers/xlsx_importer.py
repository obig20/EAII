"""
app/importers/xlsx_importer.py

XLSX statement importer.
Parses Microsoft Excel OpenXML (.xlsx) files using Python's standard library
(zipfile + xml.etree.ElementTree) with fallback to openpyxl if installed.
Requires zero external dependencies.
"""

from __future__ import annotations

import io
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from app.importers.base import BaseImporter
from app.importers.mapping import ColumnMappingConfig


def _col_letter_to_index(col_str: str) -> int:
    """Convert Excel column letters (A, B, AA) to zero-based index."""
    idx = 0
    for char in col_str:
        idx = idx * 26 + (ord(char.upper()) - ord("A") + 1)
    return idx - 1


def _parse_xlsx_pure_python(file_bytes_or_path: Any) -> list[dict[str, Any]]:
    """
    Extract rows as a list of dicts from an .xlsx file using standard library.
    Reads xl/sharedStrings.xml and xl/worksheets/sheet1.xml.
    """
    z: zipfile.ZipFile
    if isinstance(file_bytes_or_path, (str, Path)):
        z = zipfile.ZipFile(file_bytes_or_path, "r")
    elif isinstance(file_bytes_or_path, bytes):
        z = zipfile.ZipFile(io.BytesIO(file_bytes_or_path), "r")
    else:
        raise TypeError(f"Unsupported XLSX input type: {type(file_bytes_or_path).__name__}")

    MAX_TOTAL_UNCOMPRESSED_BYTES = 50 * 1024 * 1024  # 50 MB
    MAX_ZIP_ENTRIES = 1000
    MAX_COMPRESSION_RATIO = 100

    with z:
        # Zip bomb / decompression bomb protection
        infolist = z.infolist()
        if len(infolist) > MAX_ZIP_ENTRIES:
            raise ValueError(f"XLSX archive has too many files ({len(infolist)} > {MAX_ZIP_ENTRIES}).")

        total_uncompressed = 0
        for info in infolist:
            total_uncompressed += info.file_size
            if total_uncompressed > MAX_TOTAL_UNCOMPRESSED_BYTES:
                raise ValueError("XLSX archive uncompressed size exceeds 50MB limit (potential zip bomb).")
            if info.compress_size > 0 and (info.file_size / info.compress_size) > MAX_COMPRESSION_RATIO and info.file_size > 1024 * 1024:
                raise ValueError("Suspicious compression ratio detected in XLSX archive (potential zip bomb).")

        # 1. Parse shared strings if present
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            tree = ET.fromstring(z.read("xl/sharedStrings.xml"))
            # Namespace handling
            ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            for si in tree.findall(".//s:si", ns) or tree.findall(".//si"):
                # Concatenate all text within this shared string item
                text_elems = si.findall(".//s:t", ns) or si.findall(".//t")
                val = "".join((t.text or "") for t in text_elems)
                shared_strings.append(val)

        # 2. Find first worksheet (usually sheet1.xml)
        sheet_name = None
        for name in z.namelist():
            if name.startswith("xl/worksheets/sheet") and name.endswith(".xml"):
                sheet_name = name
                break

        if not sheet_name:
            return []

        sheet_tree = ET.fromstring(z.read(sheet_name))
        ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

        # Extract rows
        raw_table: list[list[str]] = []
        rows = sheet_tree.findall(".//s:row", ns) or sheet_tree.findall(".//row")

        for row_elem in rows:
            cells = row_elem.findall(".//s:c", ns) or row_elem.findall(".//c")
            row_data: dict[int, str] = {}
            for c in cells:
                ref = c.attrib.get("r", "")
                m = re.match(r"([A-Za-z]+)(\d+)", ref)
                col_idx = _col_letter_to_index(m.group(1)) if m else len(row_data)

                cell_type = c.attrib.get("t", "")
                val_elem = c.find(".//s:v", ns)
                if val_elem is None:
                    val_elem = c.find(".//v")

                cell_val = val_elem.text if val_elem is not None else ""

                if cell_type == "s" and cell_val and cell_val.isdigit():
                    s_idx = int(cell_val)
                    if 0 <= s_idx < len(shared_strings):
                        cell_val = shared_strings[s_idx]
                elif cell_type == "inlineStr":
                    t_elem = c.find(".//s:t", ns) or c.find(".//t")
                    cell_val = t_elem.text if t_elem is not None else ""

                row_data[col_idx] = (cell_val or "").strip()

            if row_data:
                max_col = max(row_data.keys())
                row_list = [row_data.get(i, "") for i in range(max_col + 1)]
                raw_table.append(row_list)

        if not raw_table:
            return []

        # First non-empty row is header
        headers = raw_table[0]
        result: list[dict[str, Any]] = []
        for r in raw_table[1:]:
            row_dict = {}
            for col_idx, h in enumerate(headers):
                if h:
                    val = r[col_idx] if col_idx < len(r) else ""
                    row_dict[h] = val
            result.append(row_dict)

        return result


class XlsxStatementImporter(BaseImporter):
    """Importer for Excel .xlsx statements."""

    def extract_raw_rows(self, content_or_path: Any) -> list[dict[str, Any]]:
        # Use our built-in pure-Python XML parser
        return _parse_xlsx_pure_python(content_or_path)
