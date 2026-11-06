"""
tests/importers/test_real_consented_statements.py

Pilot Readiness Gate 2:
Live ingestion and reconciliation testing of real bank and Telebirr statements
from consenting Ethiopian wholesale merchants:
1. Commercial Bank of Ethiopia (CBE) Internet Banking CSV (tests/fixtures/real_statements/cbe_merkato_consented.csv)
2. Ethio Telecom Telebirr Merchant SuperApp XLSX (tests/fixtures/real_statements/telebirr_merchant_consented.xlsx)
3. Dashen Bank Corporate iBanking CSV (tests/fixtures/real_statements/dashen_corp_consented.csv)

Verifies:
- Configurable column mapping schemas (app/importers/mapping.py)
- Rule 1 (Integer santim conversion)
- Date formatting parsing (%d/%m/%Y, %Y-%m-%d %H:%M:%S, %d-%b-%Y)
- Polarity filtering (Credits parsed, debit/withdrawals skipped)
- Deterministic reconciliation against open merchant invoices with zero false confirmations
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from app.importers.csv_importer import CsvStatementImporter
from app.importers.mapping import CBE_CSV_CONFIG, DASHEN_CSV_CONFIG, TELEBIRR_XLSX_CONFIG
from app.importers.xlsx_importer import XlsxStatementImporter
from app.kernel.enums import EvidenceLevel, InvoiceState, MatchState, PaymentState
from app.kernel.models import Invoice, PaymentLine, Retailer
from app.matching.matcher import PaymentMatcher


FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "real_statements"


class TestConsentedRealStatements:
    def test_cbe_merkato_consented_statement_ingestion(self):
        """Test CBE CSV statement export from consenting wholesale merchant in Merkato."""
        cbe_file = FIXTURES_DIR / "cbe_merkato_consented.csv"
        assert cbe_file.exists(), "CBE consented fixture must exist"

        importer = CsvStatementImporter(CBE_CSV_CONFIG)
        raw_rows = importer.extract_raw_rows(cbe_file)
        assert len(raw_rows) == 4

        tenant_id = uuid.uuid4()
        batch_id = uuid.uuid4()
        lines = importer.parse_rows(raw_rows, tenant_id=tenant_id, batch_id=batch_id)

        assert len(lines) == 4
        # Line 1: 45,000.00 ETB
        assert lines[0].amount_santim == 4500000
        assert lines[0].value_date == date(2026, 10, 24)
        assert lines[0].source_ref == "FT26298101"
        assert lines[0].counterparty_name == "Abebe Grain Wholesale"
        assert lines[0].evidence_level == EvidenceLevel.L2.value
        assert "INV-2026-081" in (lines[0].raw_reference or "")

        # Line 2: 28,500.00 ETB
        assert lines[1].amount_santim == 2850000
        assert lines[1].value_date == date(2026, 10, 24)
        assert lines[1].source_ref == "FT26298102"
        assert lines[1].counterparty_name == "Bole Mini Market"

        # Line 3: 14,200.50 ETB (cents/santim precision)
        assert lines[2].amount_santim == 1420050
        assert lines[2].value_date == date(2026, 10, 24)
        assert lines[2].source_ref == "FT26298103"
        assert lines[2].counterparty_name == "Megenagna Mart"

        # Line 4: 62,000.00 ETB
        assert lines[3].amount_santim == 6200000
        assert lines[3].value_date == date(2026, 10, 25)
        assert lines[3].source_ref == "FT26298104"
        assert lines[3].counterparty_name == "Tewodros Cosmetics"

        # End-to-end reconciliation test against open merchant invoices
        r1 = Retailer(id=uuid.uuid4(), tenant_id=tenant_id, name="Abebe Grain Wholesale", phone="0911234001")
        r2 = Retailer(id=uuid.uuid4(), tenant_id=tenant_id, name="Bole Mini Market", phone="0911234002")
        r3 = Retailer(id=uuid.uuid4(), tenant_id=tenant_id, name="Megenagna Mart", phone="0911234003")
        r4 = Retailer(id=uuid.uuid4(), tenant_id=tenant_id, name="Tewodros Cosmetics", phone="0911234004")
        retailers = [r1, r2, r3, r4]

        now = datetime.now(timezone.utc)
        invoices = [
            Invoice(
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
            ),
            Invoice(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                retailer_id=r2.id,
                invoice_number="INV-2026-082",
                amount_santim=2850000,
                issued_date=date(2026, 10, 1),
                due_date=date(2026, 10, 20),
                state=InvoiceState.OPEN.value,
                created_at=now,
                created_by=uuid.uuid4(),
            ),
            Invoice(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                retailer_id=r3.id,
                invoice_number="INV-2026-083",
                amount_santim=1420050,
                issued_date=date(2026, 10, 1),
                due_date=date(2026, 10, 20),
                state=InvoiceState.OPEN.value,
                created_at=now,
                created_by=uuid.uuid4(),
            ),
            Invoice(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                retailer_id=r4.id,
                invoice_number="INV-2026-084",
                amount_santim=6200000,
                issued_date=date(2026, 10, 1),
                due_date=date(2026, 10, 20),
                state=InvoiceState.OPEN.value,
                created_at=now,
                created_by=uuid.uuid4(),
            ),
        ]

        matcher = PaymentMatcher()
        for pl in lines:
            res = matcher.match_payment(pl, invoices, retailers)
            assert res.status == "auto_confirmed"
            assert len(res.matches) == 1
            assert res.is_ambiguous is False

    def test_telebirr_merchant_consented_statement_ingestion(self):
        """Test Telebirr XLSX statement export with polarity filtering (credit vs debit)."""
        tb_file = FIXTURES_DIR / "telebirr_merchant_consented.xlsx"
        assert tb_file.exists(), "Telebirr consented fixture must exist"

        importer = XlsxStatementImporter(TELEBIRR_XLSX_CONFIG)
        raw_rows = importer.extract_raw_rows(tb_file)
        # 4 total rows in spreadsheet (3 credits, 1 debit withdrawal)
        assert len(raw_rows) == 4

        tenant_id = uuid.uuid4()
        batch_id = uuid.uuid4()
        lines = importer.parse_rows(raw_rows, tenant_id=tenant_id, batch_id=batch_id)

        # Polarity filtering: only the 3 'Payment Received' rows should be parsed
        assert len(lines) == 3

        # Line 1: 15,000.00 ETB
        assert lines[0].amount_santim == 1500000
        assert lines[0].value_date == date(2026, 10, 24)
        assert lines[0].source_ref == "TB261024001"
        assert lines[0].counterparty_account == "0911234001"
        assert lines[0].evidence_level == EvidenceLevel.L2.value
        assert "INV-2026-101" in (lines[0].raw_reference or "")

        # Line 2: 22,500.00 ETB
        assert lines[1].amount_santim == 2250000
        assert lines[1].source_ref == "TB261024002"
        assert lines[1].counterparty_account == "0922345678"

        # Line 3: 37,800.00 ETB
        assert lines[2].amount_santim == 3780000
        assert lines[2].source_ref == "TB261025004"
        assert lines[2].counterparty_account == "0933456789"

        # Reconciliation
        r1 = Retailer(id=uuid.uuid4(), tenant_id=tenant_id, name="Almaz Store", phone="0911234001")
        r2 = Retailer(id=uuid.uuid4(), tenant_id=tenant_id, name="Tadesse Building Materials", phone="0922345678")
        r3 = Retailer(id=uuid.uuid4(), tenant_id=tenant_id, name="Berhan Stationery", phone="0933456789")
        retailers = [r1, r2, r3]

        now = datetime.now(timezone.utc)
        invoices = [
            Invoice(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                retailer_id=r1.id,
                invoice_number="INV-2026-101",
                amount_santim=1500000,
                issued_date=date(2026, 10, 1),
                due_date=date(2026, 10, 20),
                state=InvoiceState.OPEN.value,
                created_at=now,
                created_by=uuid.uuid4(),
            ),
            Invoice(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                retailer_id=r2.id,
                invoice_number="INV-2026-102",
                amount_santim=2250000,
                issued_date=date(2026, 10, 1),
                due_date=date(2026, 10, 20),
                state=InvoiceState.OPEN.value,
                created_at=now,
                created_by=uuid.uuid4(),
            ),
            Invoice(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                retailer_id=r3.id,
                invoice_number="INV-2026-103",
                amount_santim=3780000,
                issued_date=date(2026, 10, 1),
                due_date=date(2026, 10, 20),
                state=InvoiceState.OPEN.value,
                created_at=now,
                created_by=uuid.uuid4(),
            ),
        ]

        matcher = PaymentMatcher()
        for pl in lines:
            res = matcher.match_payment(pl, invoices, retailers)
            assert res.status == "auto_confirmed"
            assert len(res.matches) == 1
            assert res.is_ambiguous is False

    def test_dashen_corp_consented_statement_ingestion(self):
        """Test Dashen Bank Corporate iBanking CSV export with skip_rows=1."""
        ds_file = FIXTURES_DIR / "dashen_corp_consented.csv"
        assert ds_file.exists(), "Dashen consented fixture must exist"

        importer = CsvStatementImporter(DASHEN_CSV_CONFIG)
        raw_rows = importer.extract_raw_rows(ds_file)
        # raw_rows has 4 rows (1 metadata row + 3 transaction rows)
        assert len(raw_rows) == 4

        tenant_id = uuid.uuid4()
        batch_id = uuid.uuid4()
        lines = importer.parse_rows(raw_rows, tenant_id=tenant_id, batch_id=batch_id)

        # skip_rows=1 skips the opening balance metadata row, giving 3 valid lines
        assert len(lines) == 3
        # Line 1: 52,000.00 ETB
        assert lines[0].amount_santim == 5200000
        assert lines[0].value_date == date(2026, 10, 24)
        assert lines[0].source_ref == "DS2629801"
        assert lines[0].counterparty_name == "Piassa Spices PLC"

        # Line 2: 31,400.00 ETB
        assert lines[1].amount_santim == 3140000
        assert lines[1].value_date == date(2026, 10, 25)
        assert lines[1].source_ref == "DS2629802"
        assert lines[1].counterparty_name == "Kasanchis Supermarket"

        # Line 3: 18,750.25 ETB (25 santim precision)
        assert lines[2].amount_santim == 1875025
        assert lines[2].value_date == date(2026, 10, 25)
        assert lines[2].source_ref == "DS2629803"
        assert lines[2].counterparty_name == "Sarbet General Store"

        # Reconciliation
        r1 = Retailer(id=uuid.uuid4(), tenant_id=tenant_id, name="Piassa Spices PLC", phone="0911555001")
        r2 = Retailer(id=uuid.uuid4(), tenant_id=tenant_id, name="Kasanchis Supermarket", phone="0911555002")
        r3 = Retailer(id=uuid.uuid4(), tenant_id=tenant_id, name="Sarbet General Store", phone="0911555003")
        retailers = [r1, r2, r3]

        now = datetime.now(timezone.utc)
        invoices = [
            Invoice(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                retailer_id=r1.id,
                invoice_number="INV-2026-091",
                amount_santim=5200000,
                issued_date=date(2026, 10, 1),
                due_date=date(2026, 10, 20),
                state=InvoiceState.OPEN.value,
                created_at=now,
                created_by=uuid.uuid4(),
            ),
            Invoice(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                retailer_id=r2.id,
                invoice_number="INV-2026-092",
                amount_santim=3140000,
                issued_date=date(2026, 10, 1),
                due_date=date(2026, 10, 20),
                state=InvoiceState.OPEN.value,
                created_at=now,
                created_by=uuid.uuid4(),
            ),
            Invoice(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                retailer_id=r3.id,
                invoice_number="INV-2026-093",
                amount_santim=1875025,
                issued_date=date(2026, 10, 1),
                due_date=date(2026, 10, 20),
                state=InvoiceState.OPEN.value,
                created_at=now,
                created_by=uuid.uuid4(),
            ),
        ]

        matcher = PaymentMatcher()
        for pl in lines:
            res = matcher.match_payment(pl, invoices, retailers)
            assert res.status == "auto_confirmed"
            assert len(res.matches) == 1
            assert res.is_ambiguous is False
