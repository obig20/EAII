"""
tests/importers/test_sample_statement_formats.py

Integration tests for bank and mobile money statement format parsers.
Uses generated synthetic test data fixtures to verify:
- Configurable column mappings (app/importers/mapping.py)
- Integer Santim conversion (Rule 1)
- Date parsing across Ethiopian bank formats (%d/%m/%Y, %Y-%m-%d %H:%M:%S, %d-%b-%Y)
- Polarity filtering (Credits parsed, debit/withdrawals skipped)
- Synthetic matching against sample invoice fixtures
"""

from __future__ import annotations

import io
import uuid
import zipfile
from datetime import date, datetime, timezone

import pytest

from app.importers.csv_importer import CsvStatementImporter
from app.importers.mapping import CBE_CSV_CONFIG, DASHEN_CSV_CONFIG, TELEBIRR_XLSX_CONFIG
from app.importers.xlsx_importer import XlsxStatementImporter
from app.kernel.enums import EvidenceLevel, InvoiceState, PaymentState
from app.kernel.models import Invoice, PaymentLine, Retailer
from app.matching.matcher import PaymentMatcher


class TestSampleStatementFormats:
    def test_cbe_csv_sample_format_parsing(self):
        """Test CBE CSV statement parsing using synthetic sample rows."""
        sample_csv = """Posting Date,Transaction ID,Description,Reference No,Credit Amount,Counterparty Name,Counterparty Account
24/10/2026,SAMPLE-FT-001,CBE Birr Inward Transfer,INV-2026-081,45000.00,Sample Retailer A,100012345678
24/10/2026,SAMPLE-FT-002,Branch Cash Deposit,INV-2026-082,28500.00,Sample Retailer B,100098765432
24/10/2026,SAMPLE-FT-003,Mobile Transfer,INV-2026-083,14200.50,Sample Retailer C,100045678901
25/10/2026,SAMPLE-FT-004,Cheque Clearance,INV-2026-084,62000.00,Sample Retailer D,100078901234
"""
        importer = CsvStatementImporter(CBE_CSV_CONFIG)
        raw_rows = importer.extract_raw_rows(sample_csv)
        assert len(raw_rows) == 4

        tenant_id = uuid.uuid4()
        batch_id = uuid.uuid4()
        lines = importer.parse_rows(raw_rows, tenant_id=tenant_id, batch_id=batch_id)

        assert len(lines) == 4
        # Line 1: 45,000.00 ETB = 4,500,000 santim
        assert lines[0].amount_santim == 4500000
        assert lines[0].value_date == date(2026, 10, 24)
        assert lines[0].source_ref == "SAMPLE-FT-001"
        assert lines[0].counterparty_name == "Sample Retailer A"
        assert lines[0].evidence_level == EvidenceLevel.L2.value
        assert "INV-2026-081" in (lines[0].raw_reference or "")

        # Line 3: 14,200.50 ETB = 1,420,050 santim
        assert lines[2].amount_santim == 1420050
        assert lines[2].value_date == date(2026, 10, 24)
        assert lines[2].source_ref == "SAMPLE-FT-003"
        assert lines[2].counterparty_name == "Sample Retailer C"

        # Matching against synthetic invoices
        r1 = Retailer(id=uuid.uuid4(), tenant_id=tenant_id, name="Sample Retailer A", phone="0911000001")
        now = datetime.now(timezone.utc)
        inv1 = Invoice(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            retailer_id=r1.id,
            invoice_number="INV-2026-081",
            amount_santim=4500000,
            issued_date=date(2026, 10, 1),
            due_date=date(2026, 10, 20),
            state=InvoiceState.OPEN.value,
            created_at=now,
            created_by=uuid.uuid4(),
        )

        matcher = PaymentMatcher()
        res = matcher.match_payment(lines[0], [inv1], [r1])
        assert res.status == "auto_confirmed"
        assert len(res.matches) == 1

    def test_telebirr_xlsx_sample_format_parsing(self):
        """Test Telebirr XLSX format parsing with synthetic sample data and polarity filtering."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
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
            z.writestr(
                "_rels/.rels",
                """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""",
            )
            z.writestr(
                "xl/workbook.xml",
                """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheets><sheet name="Sheet1" sheetId="1" r:id="rId1" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/></sheets>
</workbook>""",
            )
            shared_strings = [
                "Transaction Time", "Receipt No", "Type", "Net Amount", "Sender Mobile", "Sender Name", "Service Name", "Remarks",
                "2026-10-24 09:15:22", "SAMPLE-TB-001", "Payment Received", "15000.00", "0911000001", "Sample Shop A", "Buy Goods", "INV-2026-101",
                "2026-10-24 16:05:44", "SAMPLE-TB-002", "Withdrawal to Bank", "5000.00", "0911000001", "Sample Shop A", "Outward", "Transfer out",
                "2026-10-25 08:30:19", "SAMPLE-TB-003", "Payment Received", "37800.00", "0933000002", "Sample Shop B", "Buy Goods", "INV-2026-103",
            ]
            si_xml = "".join(f"<si><t>{s}</t></si>" for s in shared_strings)
            z.writestr(
                "xl/sharedStrings.xml",
                f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="{len(shared_strings)}" uniqueCount="{len(shared_strings)}">
  {si_xml}
</sst>""",
            )
            sheet_rows = []
            sheet_rows.append('<row r="1">' + "".join(f'<c r="{chr(ord("A") + i)}1" t="s"><v>{i}</v></c>' for i in range(8)) + "</row>")
            for row_idx, start_s_idx in [(2, 8), (3, 16), (4, 24)]:
                cells = "".join(f'<c r="{chr(ord("A") + i)}{row_idx}" t="s"><v>{start_s_idx + i}</v></c>' for i in range(8))
                sheet_rows.append(f'<row r="{row_idx}">{cells}</row>')
            sheet_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>{''.join(sheet_rows)}</sheetData>
</worksheet>"""
            z.writestr("xl/worksheets/sheet1.xml", sheet_xml)

        importer = XlsxStatementImporter(TELEBIRR_XLSX_CONFIG)
        raw_rows = importer.extract_raw_rows(buf.getvalue())
        # 3 data rows in file
        assert len(raw_rows) == 3

        tenant_id = uuid.uuid4()
        batch_id = uuid.uuid4()
        lines = importer.parse_rows(raw_rows, tenant_id=tenant_id, batch_id=batch_id)

        # Polarity filtering: only the 2 'Payment Received' rows should be parsed
        assert len(lines) == 2
        assert lines[0].amount_santim == 1500000
        assert lines[0].source_ref == "SAMPLE-TB-001"
        assert lines[1].amount_santim == 3780000
        assert lines[1].source_ref == "SAMPLE-TB-003"

    def test_dashen_csv_sample_format_parsing(self):
        """Test Dashen Bank CSV format parsing with synthetic sample data and skip_rows=1."""
        sample_csv = """Value Date,Txn Ref,Narration,Transaction Details,Credit,Customer Name,Source Account
24-Oct-2026,SAMPLE-OPENING,Opening Balance B/F,Balance Forward,0.00,Sample Bank,001200000
24-Oct-2026,SAMPLE-DS-001,Payment settlement INV-2026-091,RTGS Inward,52000.00,Sample Trading A,001200001
25-Oct-2026,SAMPLE-DS-002,Settlement INV-2026-092,Amole Transfer,31400.00,Sample Trading B,001200002
"""
        importer = CsvStatementImporter(DASHEN_CSV_CONFIG)
        raw_rows = importer.extract_raw_rows(sample_csv)
        assert len(raw_rows) == 3

        tenant_id = uuid.uuid4()
        batch_id = uuid.uuid4()
        lines = importer.parse_rows(raw_rows, tenant_id=tenant_id, batch_id=batch_id)

        # skip_rows=1 skips opening balance row
        assert len(lines) == 2
        assert lines[0].amount_santim == 5200000
        assert lines[0].source_ref == "SAMPLE-DS-001"
        assert lines[0].counterparty_name == "Sample Trading A"
        assert lines[1].amount_santim == 3140000
        assert lines[1].source_ref == "SAMPLE-DS-002"
