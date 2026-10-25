"""
tests/importers/test_importers.py

Tests for statement importers and configurable column mapping.
Verifies parsing of amounts to integer santim (Rule 1) and statement event logging (Rule 4).
"""

from __future__ import annotations

import io
import uuid
import zipfile
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import pytest

from app.importers.base import parse_amount_santim, parse_statement_date
from app.importers.csv_importer import CsvStatementImporter
from app.importers.mapping import (
    CBE_CSV_CONFIG,
    DASHEN_CSV_CONFIG,
    TELEBIRR_XLSX_CONFIG,
    ColumnMappingConfig,
)
from app.importers.xlsx_importer import XlsxStatementImporter
from app.kernel.enums import EvidenceLevel


# ---------------------------------------------------------------------------
# Amount & Date Parsing Tests (Rule 1: integer santim only)
# ---------------------------------------------------------------------------
class TestAmountAndDateParsing:
    def test_parse_amount_standard(self):
        assert parse_amount_santim("125.50") == 12550
        assert parse_amount_santim("1,250.00") == 125000
        assert parse_amount_santim("500") == 50000
        assert parse_amount_santim("100.00 ETB") == 10000

    def test_parse_amount_raw_int(self):
        # Raw integer passed (e.g. 100 Birr -> 10000 santim)
        assert parse_amount_santim(100) == 10000

    def test_parse_amount_rejects_negative_or_zero(self):
        with pytest.raises(ValueError):
            parse_amount_santim("0.00")
        with pytest.raises(ValueError):
            parse_amount_santim("-50.00")

    def test_parse_dates(self):
        assert parse_statement_date("2026-10-25") == date(2026, 10, 25)
        assert parse_statement_date("25/10/2026", "%d/%m/%Y") == date(2026, 10, 25)
        assert parse_statement_date("25-Oct-2026", "%d-%b-%Y") == date(2026, 10, 25)


# ---------------------------------------------------------------------------
# Config Loading
# ---------------------------------------------------------------------------
class TestColumnMappingConfig:
    def test_load_default_configs(self):
        assert CBE_CSV_CONFIG.source_name == "cbe_csv"
        assert CBE_CSV_CONFIG.evidence_level == EvidenceLevel.L2
        assert TELEBIRR_XLSX_CONFIG.source_name == "telebirr_xlsx"

    def test_load_json_configs(self):
        config_dir = Path(__file__).parent.parent.parent / "app" / "importers" / "configs"
        cbe_file = config_dir / "cbe_csv.json"
        if cbe_file.exists():
            cfg = ColumnMappingConfig.from_json_file(cbe_file)
            assert cfg.source_name == "cbe_csv"
            assert cfg.amount_column == "Credit Amount"


# ---------------------------------------------------------------------------
# CSV Statement Importer
# ---------------------------------------------------------------------------
class TestCsvStatementImporter:
    def test_import_cbe_csv(self):
        csv_data = """Posting Date,Transaction ID,Description,Reference No,Credit Amount,Counterparty Name,Counterparty Account
25/10/2026,FT1001,Payment from Retailer,INV-001,15000.00,Abebe Shop,0911234001
25/10/2026,FT1002,Cash In,INV-002,24000.50,Almaz Store,0911234002
"""
        importer = CsvStatementImporter(CBE_CSV_CONFIG)
        raw_rows = importer.extract_raw_rows(csv_data)
        assert len(raw_rows) == 2

        tid = uuid.uuid4()
        batch_id = uuid.uuid4()
        lines = importer.parse_rows(raw_rows, tenant_id=tid, batch_id=batch_id)

        assert len(lines) == 2
        assert lines[0].amount_santim == 1500000  # 15,000 ETB = 1,500,000 santim
        assert lines[0].value_date == date(2026, 10, 25)
        assert lines[0].source_ref == "FT1001"
        assert lines[0].counterparty_name == "Abebe Shop"
        assert lines[0].evidence_level == "L2"
        assert "INV-001" in (lines[0].raw_reference or "")

        assert lines[1].amount_santim == 2400050  # 24,000.50 ETB = 2,400,050 santim

    def test_polarity_filtering(self):
        # Tests skipping Debit / withdrawal rows
        config = ColumnMappingConfig(
            source_name="test_polarity",
            date_column="Date",
            amount_column="Amount",
            polarity_column="Type",
            credit_value="CR",
        )
        csv_data = """Date,Type,Amount
2026-10-25,CR,100.00
2026-10-25,DR,50.00
2026-10-25,CR,200.00
"""
        importer = CsvStatementImporter(config)
        raw_rows = importer.extract_raw_rows(csv_data)
        lines = importer.parse_rows(raw_rows, tenant_id=uuid.uuid4(), batch_id=uuid.uuid4())

        assert len(lines) == 2
        assert lines[0].amount_santim == 10000
        assert lines[1].amount_santim == 20000


# ---------------------------------------------------------------------------
# XLSX Statement Importer (Pure Python standard library test)
# ---------------------------------------------------------------------------
class TestXlsxStatementImporter:
    def _create_minimal_xlsx(self) -> bytes:
        """Create a minimal valid OpenXML XLSX in-memory for testing."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            # [Content_Types].xml
            z.writestr(
                "[Content_Types].xml",
                """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>
</Types>""",
            )

            # _rels/.rels
            z.writestr(
                "_rels/.rels",
                """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""",
            )

            # xl/workbook.xml
            z.writestr(
                "xl/workbook.xml",
                """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheets><sheet name="Sheet1" sheetId="1" r:id="rId1" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/></sheets>
</workbook>""",
            )

            # xl/sharedStrings.xml
            shared_strings = [
                "Transaction Time", "Receipt No", "Type", "Net Amount", "Sender Mobile", "Remarks",
                "2026-10-25 14:30:00", "TB12345", "Payment Received", "5000.00", "0911234001", "INV-050",
            ]
            si_xml = "".join(f"<si><t>{s}</t></si>" for s in shared_strings)
            z.writestr(
                "xl/sharedStrings.xml",
                f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="{len(shared_strings)}" uniqueCount="{len(shared_strings)}">
  {si_xml}
</sst>""",
            )

            # xl/worksheets/sheet1.xml
            z.writestr(
                "xl/worksheets/sheet1.xml",
                """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="1">
      <c r="A1" t="s"><v>0</v></c>
      <c r="B1" t="s"><v>1</v></c>
      <c r="C1" t="s"><v>2</v></c>
      <c r="D1" t="s"><v>3</v></c>
      <c r="E1" t="s"><v>4</v></c>
      <c r="F1" t="s"><v>5</v></c>
    </row>
    <row r="2">
      <c r="A2" t="s"><v>6</v></c>
      <c r="B2" t="s"><v>7</v></c>
      <c r="C2" t="s"><v>8</v></c>
      <c r="D2" t="s"><v>9</v></c>
      <c r="E2" t="s"><v>10</v></c>
      <c r="F2" t="s"><v>11</v></c>
    </row>
  </sheetData>
</worksheet>""",
            )

        return buf.getvalue()

    def test_import_telebirr_xlsx(self):
        xlsx_bytes = self._create_minimal_xlsx()
        importer = XlsxStatementImporter(TELEBIRR_XLSX_CONFIG)
        raw_rows = importer.extract_raw_rows(xlsx_bytes)
        assert len(raw_rows) == 1

        tid = uuid.uuid4()
        batch_id = uuid.uuid4()
        lines = importer.parse_rows(raw_rows, tenant_id=tid, batch_id=batch_id)

        assert len(lines) == 1
        assert lines[0].amount_santim == 500000  # 5,000 ETB
        assert lines[0].source_ref == "TB12345"
        assert lines[0].counterparty_account == "0911234001"
        assert "INV-050" in (lines[0].raw_reference or "")
        assert lines[0].evidence_level == "L2"
