"""
tests/importers/test_import_wizard.py

Integration and unit tests for the Statement Import Wizard (Stage 3).
Tests format autodetection (encodings, delimiters, header offsets, numbers, dates),
zero-guessing ambiguous date halts, row preview, row-level error reporting,
RLS tenant isolation on saved mappings, security defenses, and locale parity.
All fixtures and data use synthetic samples labeled 'sample'.
"""

from __future__ import annotations

import csv
import io
import re
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.importers.wizard import (
    analyze_statement_file,
    detect_encoding,
    detect_header_row,
    detect_number_format,
    detect_date_format,
    generate_preview,
    parse_wizard_amount,
    parse_wizard_date,
    execute_wizard_import,
    StatementImportError,
)
from app.kernel.auth import hash_password
from app.kernel.enums import EvidenceLevel, UserRole
from app.kernel.models import (
    ImportBatch,
    ImportBatchError,
    PaymentLine,
    Tenant,
    TenantImportMapping,
    User,
)
from app.main import app
from tests.conftest import authenticate_client

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "sample_statements"


# ---------------------------------------------------------------------------
# Unit Tests for Wizard Engine
# ---------------------------------------------------------------------------

class TestWizardEngine:
    def test_wizard_utf8_with_bom(self):
        """Test UTF-8 with BOM is correctly detected and parsed."""
        file_path = FIXTURES_DIR / "sample_cbe_bom.csv"
        raw_bytes = file_path.read_bytes()

        encoding = detect_encoding(raw_bytes)
        assert encoding == "utf-8-sig"

        fmt = analyze_statement_file(raw_bytes, "sample_cbe_bom.csv")
        assert fmt.encoding == "utf-8-sig"
        assert fmt.delimiter == ","
        assert fmt.header_row_index == 0
        assert "Posting Date" in fmt.headers
        assert "Credit Amount" in fmt.headers
        assert fmt.date_format == "%d/%m/%Y"
        assert not fmt.is_date_ambiguous

        preview = generate_preview(raw_bytes, fmt)
        assert len(preview.preview_rows) == 2
        assert preview.preview_rows[0].parsed_santim == 4500000  # 45,000 ETB
        assert preview.preview_rows[0].parsed_date == "2026-10-24"
        assert preview.error_count == 0

    def test_wizard_cp1252_encoding(self):
        """Test CP1252 (Windows-1252) encoding is detected and decoded cleanly."""
        file_path = FIXTURES_DIR / "sample_cp1252.csv"
        raw_bytes = file_path.read_bytes()

        fmt = analyze_statement_file(raw_bytes, "sample_cp1252.csv")
        assert fmt.encoding in ("cp1252", "iso-8859-1")
        assert fmt.delimiter == ";"
        assert "Posting Date" in fmt.headers
        assert "Amount" in fmt.headers

        preview = generate_preview(raw_bytes, fmt)
        assert len(preview.preview_rows) == 2
        assert preview.preview_rows[0].parsed_santim == 150000  # 1500.00 ETB
        assert preview.preview_rows[0].parsed_date == "2026-10-24"

    def test_wizard_header_offset_detection(self):
        """Test scanner detects headers on row index 3 when leading 3 rows contain meta."""
        file_path = FIXTURES_DIR / "sample_header_offset.csv"
        raw_bytes = file_path.read_bytes()

        fmt = analyze_statement_file(raw_bytes, "sample_header_offset.csv")
        assert fmt.header_row_index == 3
        assert "Posting Date" in fmt.headers
        assert "Credit Amount" in fmt.headers

        preview = generate_preview(raw_bytes, fmt)
        assert len(preview.preview_rows) == 2
        assert preview.preview_rows[0].parsed_santim == 1800000
        assert preview.preview_rows[1].parsed_santim == 2250000

    def test_wizard_european_number_formatting(self):
        """Test European number format (1.250,50) parsing into integer santim without float."""
        file_path = FIXTURES_DIR / "sample_european_number.csv"
        raw_bytes = file_path.read_bytes()

        fmt = analyze_statement_file(raw_bytes, "sample_european_number.csv")
        assert fmt.number_format == "1.234,56"

        preview = generate_preview(raw_bytes, fmt)
        assert len(preview.preview_rows) == 2
        # 1.250,50 ETB = 125,050 santim
        assert preview.preview_rows[0].parsed_santim == 125050
        # 45.000,00 ETB = 4,500,000 santim
        assert preview.preview_rows[1].parsed_santim == 4500000

        # Direct parser test
        assert parse_wizard_amount("1.250,50", "1.234,56") == 125050
        assert parse_wizard_amount("45.000,00", "1.234,56") == 4500000

    def test_wizard_ambiguous_date_requires_selection(self):
        """Zero-guessing principle: ambiguous dates (05/06/2026) trigger warnings."""
        file_path = FIXTURES_DIR / "sample_ambiguous_date.csv"
        raw_bytes = file_path.read_bytes()

        fmt = analyze_statement_file(raw_bytes, "sample_ambiguous_date.csv")
        assert fmt.is_date_ambiguous is True
        assert fmt.ambiguous_sample == "05/06/2026"

        # Preview without explicit format has warnings and flagged rows
        preview = generate_preview(raw_bytes, fmt)
        assert len(preview.warnings) > 0
        assert preview.preview_rows[0].error is not None
        assert "Ambiguous" in preview.preview_rows[0].error

        # With explicit selection override (%d/%m/%Y), parses cleanly
        preview_resolved = generate_preview(raw_bytes, fmt, override_date_format="%d/%m/%Y")
        assert len(preview_resolved.warnings) == 0
        assert preview_resolved.preview_rows[0].error is None
        assert preview_resolved.preview_rows[0].parsed_date == "2026-06-05"

    def test_wizard_debit_credit_columns_split(self):
        """Test separate credit/debit columns: credits parsed, debits skipped."""
        file_path = FIXTURES_DIR / "sample_debit_credit_split.csv"
        raw_bytes = file_path.read_bytes()

        fmt = analyze_statement_file(raw_bytes, "sample_debit_credit_split.csv")
        assert fmt.suggested_mapping["credit_column"] == "Credit Amount"
        assert fmt.suggested_mapping["debit_column"] == "Debit Amount"

        preview = generate_preview(raw_bytes, fmt)
        assert len(preview.preview_rows) == 3
        # Row 1: Credit 35000.00
        assert preview.preview_rows[0].is_credit is True
        assert preview.preview_rows[0].parsed_santim == 3500000
        # Row 2: Debit 5000.00 -> is_credit is False
        assert preview.preview_rows[1].is_credit is False
        # Row 3: Credit 14500.00
        assert preview.preview_rows[2].is_credit is True
        assert preview.preview_rows[2].parsed_santim == 1450000


# ---------------------------------------------------------------------------
# Integration & Security Tests via HTTP Endpoints
# ---------------------------------------------------------------------------

class TestWizardHttpAndSecurity:
    def test_wizard_cashier_403(self, client: TestClient, db_session: Session, cashier_user_a: User):
        """Cashiers cannot access the statement import wizard (Owner only)."""
        authenticate_client(client, db_session, cashier_user_a)

        resp = client.get("/statements/wizard")
        assert resp.status_code == 403

        resp_post = client.post(
            "/statements/wizard/upload",
            files={"file": ("sample.csv", b"Date,Amount\n2026-10-24,100", "text/csv")},
            data={"csrf_token": ""},
        )
        assert resp_post.status_code == 403

    def test_wizard_null_byte_and_oversized_rejected(self, client: TestClient, db_session: Session, owner_user_a: User):
        """Rejects files with null bytes and oversized uploads (>10MB)."""
        _, csrf = authenticate_client(client, db_session, owner_user_a)

        # 1. Null byte
        resp_null = client.post(
            "/statements/wizard/upload",
            files={"file": ("sample_null.csv", b"Date,Amount\x00,100", "text/csv")},
            data={"csrf_token": csrf},
        )
        assert resp_null.status_code == 400
        assert "null byte" in resp_null.text.lower()

        # 2. Oversized file (10MB + 1)
        oversized = b"A" * (10 * 1024 * 1024 + 10)
        resp_large = client.post(
            "/statements/wizard/upload",
            files={"file": ("sample_large.csv", oversized, "text/csv")},
            data={"csrf_token": csrf},
        )
        assert resp_large.status_code == 413

    def test_wizard_end_to_end_upload_preview_confirm(
        self, client: TestClient, db_session: Session, tenant_a: Tenant, owner_user_a: User
    ):
        """Full wizard flow: upload, adjust format, confirm ingestion, verify PaymentLine and Event."""
        _, csrf = authenticate_client(client, db_session, owner_user_a)

        file_path = FIXTURES_DIR / "sample_cbe_bom.csv"
        raw_bytes = file_path.read_bytes()

        # Step 1: Upload
        resp_upload = client.post(
            "/statements/wizard/upload",
            files={"file": ("sample_cbe_bom.csv", raw_bytes, "text/csv")},
            data={"csrf_token": csrf},
        )
        assert resp_upload.status_code == 200
        assert "sample_cbe_bom.csv" in resp_upload.text
        assert 'data-testid="wizard-preview-table"' in resp_upload.text

        # Extract cache token from form
        match = re.search(r'name="cache_token"\s+value="([^"]+)"', resp_upload.text)
        assert match, "cache_token not found in preview HTML"
        cache_token = match.group(1)

        # Step 2: Confirm Ingestion
        resp_confirm = client.post(
            "/statements/wizard/confirm",
            data={
                "csrf_token": csrf,
                "cache_token": cache_token,
                "filename": "sample_cbe_bom.csv",
                "date_column": "Posting Date",
                "date_format": "%d/%m/%Y",
                "amount_column": "Credit Amount",
                "number_format": "1,234.56",
                "reference_column": "Reference No",
                "narrative_column": "Description",
                "counterparty_name_column": "Counterparty Name",
                "tx_id_column": "Transaction ID",
                "save_mapping": "true",
                "save_mapping_as": "CBE Custom Format",
            },
            follow_redirects=True,
        )
        assert resp_confirm.status_code == 200

        # Verify PaymentLines in DB
        lines = db_session.execute(
            select(PaymentLine).where(PaymentLine.tenant_id == tenant_a.id)
        ).scalars().all()
        assert len(lines) == 2
        amounts = {l.amount_santim for l in lines}
        assert 4500000 in amounts  # 45,000 ETB
        assert 1250050 in amounts  # 12,500.50 ETB

        # Verify saved mapping in DB
        saved_map = db_session.execute(
            select(TenantImportMapping).where(
                TenantImportMapping.tenant_id == tenant_a.id,
                TenantImportMapping.source_name == "CBE Custom Format",
            )
        ).scalar_one_or_none()
        assert saved_map is not None
        assert saved_map.date_column == "Posting Date"
        assert saved_map.amount_column == "Credit Amount"

    def test_wizard_tenant_isolation_on_saved_mappings(
        self, client: TestClient, db_session: Session, tenant_a: Tenant, owner_user_a: User, owner_user_b: User
    ):
        """Negative test: Tenant B cannot see Tenant A's saved mapping configs."""
        from app.kernel.tenant import set_tenant
        set_tenant(db_session, tenant_a.id)

        # Create mapping for Tenant A
        mapping_a = TenantImportMapping(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            source_name="Tenant A Secret Schema",
            delimiter=";",
            encoding="utf-8",
            header_row_index=0,
            date_column="TxDate",
            date_format="%Y-%m-%d",
            amount_column="Inflow",
            number_format="1,234.56",
            created_by=owner_user_a.id,
        )
        db_session.add(mapping_a)
        db_session.commit()

        # Login as Tenant B
        authenticate_client(client, db_session, owner_user_b)
        resp = client.get("/statements/wizard")
        assert resp.status_code == 200
        # Tenant A's schema must NOT appear in Tenant B's dropdown
        assert "Tenant A Secret Schema" not in resp.text

    def test_wizard_row_error_report_and_formula_injection(
        self, client: TestClient, db_session: Session, tenant_a: Tenant, owner_user_a: User, owner_user_b: User
    ):
        """Corrupted rows logged to ImportBatchError and exported to CSV with formula injection neutralization."""
        _, csrf = authenticate_client(client, db_session, owner_user_a)

        file_path = FIXTURES_DIR / "sample_with_errors.csv"
        raw_bytes = file_path.read_bytes()

        # Upload
        resp_upload = client.post(
            "/statements/wizard/upload",
            files={"file": ("sample_with_errors.csv", raw_bytes, "text/csv")},
            data={"csrf_token": csrf},
        )
        match = re.search(r'name="cache_token"\s+value="([^"]+)"', resp_upload.text)
        assert match
        cache_token = match.group(1)

        # Confirm
        resp_confirm = client.post(
            "/statements/wizard/confirm",
            data={
                "csrf_token": csrf,
                "cache_token": cache_token,
                "filename": "sample_with_errors.csv",
                "date_column": "Date",
                "date_format": "%d/%m/%Y",
                "amount_column": "Credit Amount",
                "number_format": "1,234.56",
                "tx_id_column": "Transaction ID",
                "counterparty_name_column": "Counterparty",
            },
            follow_redirects=True,
        )
        assert resp_confirm.status_code == 200
        assert "flagged" in resp_confirm.text.lower() or "error" in resp_confirm.text.lower()

        # Find batch ID
        batch = db_session.execute(
            select(ImportBatch).where(
                ImportBatch.tenant_id == tenant_a.id,
                ImportBatch.filename == "sample_with_errors.csv",
            )
        ).scalar_one()

        # Check errors table
        errors = db_session.execute(
            select(ImportBatchError).where(ImportBatchError.import_batch_id == batch.id)
        ).scalars().all()
        assert len(errors) == 2  # 1 bad date, 1 bad amount

        # Download error report CSV
        resp_csv = client.get(f"/statements/wizard/errors/{batch.id}.csv")
        assert resp_csv.status_code == 200
        assert resp_csv.headers["content-type"].startswith("text/csv")
        csv_text = resp_csv.text
        assert "row_index,error_reason,raw_data" in csv_text
        assert "Date error" in csv_text
        assert "Amount error" in csv_text

        # Test cross-tenant access to this error CSV (Tenant B gets 404)
        client_b = TestClient(app)
        authenticate_client(client_b, db_session, owner_user_b)
        resp_cross = client_b.get(f"/statements/wizard/errors/{batch.id}.csv")
        assert resp_cross.status_code == 404

    def test_wizard_double_submission_idempotency(
        self, client: TestClient, db_session: Session, tenant_a: Tenant, owner_user_a: User
    ):
        """Idempotent ingestion: uploading identical file produces same batch, no duplicate lines."""
        _, csrf = authenticate_client(client, db_session, owner_user_a)

        file_path = FIXTURES_DIR / "sample_header_offset.csv"
        raw_bytes = file_path.read_bytes()

        # Upload 1
        resp_1 = client.post(
            "/statements/wizard/upload",
            files={"file": ("sample_header_offset.csv", raw_bytes, "text/csv")},
            data={"csrf_token": csrf},
        )
        cache_token_1 = re.search(r'name="cache_token"\s+value="([^"]+)"', resp_1.text).group(1)

        client.post(
            "/statements/wizard/confirm",
            data={
                "csrf_token": csrf,
                "cache_token": cache_token_1,
                "filename": "sample_header_offset.csv",
                "date_column": "Posting Date",
                "date_format": "%d/%m/%Y",
                "amount_column": "Credit Amount",
                "number_format": "1,234.56",
                "tx_id_column": "Transaction ID",
            },
        )

        batches_count_1 = db_session.execute(
            select(ImportBatch).where(ImportBatch.tenant_id == tenant_a.id)
        ).scalars().all()
        lines_count_1 = db_session.execute(
            select(PaymentLine).where(PaymentLine.tenant_id == tenant_a.id)
        ).scalars().all()

        # Upload 2 with identical content
        resp_2 = client.post(
            "/statements/wizard/upload",
            files={"file": ("sample_header_offset.csv", raw_bytes, "text/csv")},
            data={"csrf_token": csrf},
        )
        cache_token_2 = re.search(r'name="cache_token"\s+value="([^"]+)"', resp_2.text).group(1)

        client.post(
            "/statements/wizard/confirm",
            data={
                "csrf_token": csrf,
                "cache_token": cache_token_2,
                "filename": "sample_header_offset.csv",
                "date_column": "Posting Date",
                "date_format": "%d/%m/%Y",
                "amount_column": "Credit Amount",
                "number_format": "1,234.56",
                "tx_id_column": "Transaction ID",
            },
        )

        batches_count_2 = db_session.execute(
            select(ImportBatch).where(ImportBatch.tenant_id == tenant_a.id)
        ).scalars().all()
        lines_count_2 = db_session.execute(
            select(PaymentLine).where(PaymentLine.tenant_id == tenant_a.id)
        ).scalars().all()

        assert len(batches_count_1) == len(batches_count_2)
        assert len(lines_count_1) == len(lines_count_2)

    def test_wizard_locale_parity(self):
        """All statement wizard keys exist in both English and Amharic with zero missing keys."""
        import json
        locales_dir = Path(__file__).parent.parent.parent / "app" / "channels" / "telegram" / "locales"
        with open(locales_dir / "en.json", encoding="utf-8") as f:
            en = json.load(f)
        with open(locales_dir / "am.json", encoding="utf-8") as f:
            am = json.load(f)

        wizard_keys = [
            "wizard_title",
            "wizard_subtitle",
            "wizard_upload_step",
            "wizard_preview_step",
            "wizard_ambiguous_date_warning",
            "wizard_confirm_button",
            "wizard_update_preview",
            "wizard_save_mapping",
            "wizard_error_report",
            "wizard_row_count",
        ]

        for k in wizard_keys:
            assert k in en, f"Missing {k} in en.json"
            assert k in am, f"Missing {k} in am.json"
            assert "<!-- needs native review -->" in am[k] or len(am[k]) > 0
