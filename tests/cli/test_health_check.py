"""
tests/cli/test_health_check.py

Comprehensive tests for Stage 3: Collections Health Check.
Verifies:
  - End-to-end execution on real-looking messy data (odd formats, commas, currency strings, Amharic).
  - All 8 diagnostic sections: aging buckets, top 10 debtors, matching stats, duplicate/overpayments,
    sequence gaps, unidentified deposits, average days-to-collect DSO, 'Fix These First' list.
  - Evidence level attribution (L1, L2, L3, L4).
  - Explicit uncertainty labeling ([Uncertain] / [ያልተረጋገጠ]).
  - Zero external CDN / fonts / scripts (100% self-contained offline HTML).
  - Bilingual support ('en' and 'am').
  - Friendly column mapping errors and custom --map JSON parsing.
  - Temporary tenant lifecycle: rollback when keep=False, persistence when keep=True.
  - CLI command invocation.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.cli import main
from app.kernel.db import session_scope
from app.kernel.models import Tenant
from app.reports.health_check import (
    FriendlyMappingError,
    resolve_invoice_mapping,
    resolve_statement_mapping,
    run_health_check,
)


@pytest.fixture(autouse=True)
def ensure_tables(tables):
    """Ensure database tables and migrations are present."""
    pass


@pytest.fixture
def messy_data_dir(tmp_path: Path):
    """Generate realistic messy CSVs with Ethiopian names, currency signs, commas, and odd dates."""
    data_dir = tmp_path / "messy_data"
    data_dir.mkdir()

    # Invoices CSV: missing sequence numbers (gaps: INV-003, INV-004), Amharic names, commas
    inv_csv = data_dir / "invoices.csv"
    inv_content = (
        "Invoice No,Customer,Amount (ETB),Date,Due Date,Phone\n"
        "INV-001,ተስፋዬ የጅምላ ንግድ,\"45,000.50 ETB\",15/08/2026,15/09/2026,+251911223344\n"
        "INV-002,Almaz Grocery Store,\"12,000.00\",2026-08-10,2026-09-10,+251912345678\n"
        "INV-005,Abebe Kebede Retail,85000,2026-07-01,2026-07-31,0922112233\n"
        "INV-006,Kazanchis Mini Market,\"30,000 ETB\",2026-05-01,2026-06-01,+251930123456\n"
    )
    inv_csv.write_text(inv_content, encoding="utf-8-sig")

    # Bank statement CSV: CBE format
    stmt_csv = data_dir / "cbe_statement.csv"
    stmt_content = (
        "Posting Date,Reference No,Description,Counterparty Name,Credit Amount\n"
        "16/08/2026,FT26228A,Payment for INV-001,Tesfaye Wholesale,\"45,000.50\"\n"
        "18/08/2026,FT26230B,Partial settlement INV-002,Almaz Store,\"15,000.00\"\n"
        "20/08/2026,FT26235C,Cash Deposit Unknown,Direct Dep,8000.00\n"
        "22/08/2026,FT26240D,Direct Transfer,Unknown Payer,5000.00\n"
        "22/08/2026,FT26240D,Direct Transfer,Unknown Payer,5000.00\n"
    )
    stmt_csv.write_text(stmt_content, encoding="utf-8-sig")

    return {
        "dir": data_dir,
        "invoices": inv_csv,
        "statements": [stmt_csv],
    }


class TestCollectionsHealthCheck:

    def test_health_check_e2e_messy_data(self, messy_data_dir, tmp_path):
        """Verify full diagnostic execution and HTML content on messy data."""
        out_html = tmp_path / "collections_health_report.html"

        result = run_health_check(
            invoices_path=messy_data_dir["invoices"],
            statements_paths=messy_data_dir["statements"],
            out_path=out_html,
            lang="en",
            as_of_date=date(2026, 9, 15),
            keep=False,
        )

        assert out_html.is_file()
        content = out_html.read_text(encoding="utf-8")

        # 1. Branding, Confidentiality, and Zero CDN
        assert "CONFIDENTIAL" in content
        assert "Prepared by Birana Technology" in content
        assert "Daily Close" in content
        assert "http://" not in content
        assert "https://" not in content  # Zero external assets/fonts/scripts

        # 2. Aging Buckets present
        assert "Current (Not Due)" in content
        assert "1–30 Days Overdue" in content
        assert "31–60 Days Overdue" in content
        assert "61–90 Days Overdue" in content
        assert "90+ Days Overdue" in content

        # 3. Top Overdue Retailers
        assert "Top 10 Overdue Retailers" in content
        assert "Abebe Kebede Retail" in content or "Kazanchis Mini Market" in content

        # 4. Reconciliation Health
        assert "Auto-Confirmed (Tier A)" in content
        assert "Queued for Review" in content
        assert "Unmatched Inflows" in content

        # 5. Sequence Gaps Detected (INV-003, INV-004)
        assert "INV-003" in content
        assert "INV-004" in content
        assert "Sequence Gaps" in content

        # 6. Duplicates & Overpayments (INV-002 had 15,000 payment on 12,000 invoice)
        assert "Possible Duplicate" in content or "Overpayments" in content
        assert "Unallocated" in content or "Excess" in content

        # 7. Unidentified Deposits & Evidence Levels
        assert "Unidentified Bank Deposits" in content
        assert "L2" in content
        assert "[Uncertain]" in content

        # 8. 'Fix These First' List
        assert "Fix These First" in content
        assert len(result["action_items"]) > 0

    def test_health_check_amharic_locale(self, messy_data_dir, tmp_path):
        """Verify report renders authentic Amharic locale strings when --lang am."""
        out_html = tmp_path / "report_am.html"

        run_health_check(
            invoices_path=messy_data_dir["invoices"],
            statements_paths=messy_data_dir["statements"],
            out_path=out_html,
            lang="am",
            as_of_date=date(2026, 9, 15),
            keep=False,
        )

        content = out_html.read_text(encoding="utf-8")
        assert "ሚስጥራዊ" in content
        assert "የብድርና ሂሳብ አሰባሰብ ምርመራ ሪፖርት" in content
        assert "በቢራና ቴክኖሎጂ የተዘጋጀ" in content
        assert "ቅድሚያ የሚሰጣቸው እርምጃዎች" in content
        assert "የደንበኞች የብድር ዕድሜ ክፍፍል" in content
        assert "ያልተረጋገጠ" in content

    def test_friendly_mapping_error_on_missing_columns(self, tmp_path):
        """Verify helpful error with detected headers and --map instructions when headers fail."""
        bad_inv = tmp_path / "bad_invoices.csv"
        bad_inv.write_text("Alpha,Beta,Gamma\n1,2,3\n", encoding="utf-8")

        bad_stmt = tmp_path / "bad_statement.csv"
        bad_stmt.write_text("Foo,Bar,Baz\n1,2,3\n", encoding="utf-8")

        with pytest.raises(FriendlyMappingError) as exc_info:
            run_health_check(
                invoices_path=bad_inv,
                statements_paths=[bad_stmt],
                out_path=tmp_path / "out.html",
            )

        err_msg = str(exc_info.value)
        assert "unrecognized column headers" in err_msg
        assert "Missing required fields" in err_msg
        assert "--map <mapping.json>" in err_msg

    def test_custom_map_json(self, tmp_path):
        """Verify custom column mapping via --map JSON resolves non-standard files."""
        custom_inv = tmp_path / "custom_invoices.csv"
        custom_inv.write_text(
            "SlipCode,Buyer,Price,CreatedOn\n"
            "SL-101,Girma Store,25000,2026-08-01\n",
            encoding="utf-8",
        )

        custom_stmt = tmp_path / "custom_stmt.csv"
        custom_stmt.write_text(
            "TxDate,PaidIn,Narration\n"
            "2026-08-05,25000,SL-101 payment\n",
            encoding="utf-8",
        )

        map_file = tmp_path / "mapping.json"
        map_content = {
            "invoices": {
                "invoice_number": "SlipCode",
                "retailer_name": "Buyer",
                "amount": "Price",
                "issue_date": "CreatedOn",
            },
            "statements": {
                "date_column": "TxDate",
                "amount_column": "PaidIn",
                "reference_column": "Narration",
            },
        }
        map_file.write_text(json.dumps(map_content), encoding="utf-8")

        out_html = tmp_path / "mapped_report.html"
        result = run_health_check(
            invoices_path=custom_inv,
            statements_paths=[custom_stmt],
            out_path=out_html,
            map_path=map_file,
            keep=False,
        )

        assert out_html.is_file()
        assert result["meta"]["total_invoices_count"] == 1
        assert result["matching"]["auto_confirmed"]["count"] == 1

    def test_temporary_tenant_lifecycle(self, messy_data_dir, tmp_path):
        """Verify temporary tenant is rolled back by default and retained when keep=True."""
        # 1. Default (keep=False)
        res_default = run_health_check(
            invoices_path=messy_data_dir["invoices"],
            statements_paths=messy_data_dir["statements"],
            out_path=tmp_path / "rep1.html",
            keep=False,
        )
        tenant_name = res_default["meta"]["tenant_name"]
        with session_scope() as session:
            t = session.execute(
                select(Tenant).where(Tenant.name == tenant_name)
            ).scalar_one_or_none()
            assert t is None, "Temporary tenant should be completely rolled back when keep=False"

        # 2. Retained (keep=True)
        res_kept = run_health_check(
            invoices_path=messy_data_dir["invoices"],
            statements_paths=messy_data_dir["statements"],
            out_path=tmp_path / "rep2.html",
            keep=True,
        )
        kept_tenant_name = res_kept["meta"]["tenant_name"]
        with session_scope() as session:
            t = session.execute(
                select(Tenant).where(Tenant.name == kept_tenant_name)
            ).scalar_one_or_none()
            assert t is not None, "Temporary tenant should be retained when keep=True"
            assert t.is_demo is True

    def test_cli_health_check_command(self, messy_data_dir, tmp_path, monkeypatch, capsys):
        """Verify python -m app.cli health-check CLI interface."""
        out_file = str(tmp_path / "cli_report.html")
        args = [
            "health-check",
            "--invoices", str(messy_data_dir["invoices"]),
            "--statements", str(messy_data_dir["statements"][0]),
            "--out", out_file,
            "--lang", "en",
        ]

        monkeypatch.setattr("sys.argv", ["app.cli"] + args)
        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "Collections Health Check completed successfully!" in captured.out
        assert os.path.exists(out_file)

    def test_health_check_multiple_statements_with_xlsx(self, messy_data_dir, tmp_path):
        """Verify health check ingests multiple statements including an OpenXML XLSX file."""
        import io, zipfile

        # Generate a minimal valid XLSX statement
        xlsx_buf = io.BytesIO()
        with zipfile.ZipFile(xlsx_buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(
                "[Content_Types].xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
                '</Types>',
            )
            z.writestr(
                "_rels/.rels",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                '</Relationships>',
            )
            z.writestr(
                "xl/workbook.xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/></sheets>'
                '</workbook>',
            )
            shared_strings = [
                "Transaction Time", "Receipt No", "Type", "Net Amount", "Sender Mobile", "Remarks",
                "2026-08-25 10:00:00", "TB99991", "Payment Received", "85000", "0922112233", "Settlement for INV-005",
            ]
            si_xml = "".join(f"<si><t>{s}</t></si>" for s in shared_strings)
            z.writestr(
                "xl/sharedStrings.xml",
                f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="{len(shared_strings)}" uniqueCount="{len(shared_strings)}">{si_xml}</sst>',
            )
            z.writestr(
                "xl/worksheets/sheet1.xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                '<sheetData>'
                '<row r="1">'
                '<c r="A1" t="s"><v>0</v></c>'
                '<c r="B1" t="s"><v>1</v></c>'
                '<c r="C1" t="s"><v>2</v></c>'
                '<c r="D1" t="s"><v>3</v></c>'
                '<c r="E1" t="s"><v>4</v></c>'
                '<c r="F1" t="s"><v>5</v></c>'
                '</row>'
                '<row r="2">'
                '<c r="A2" t="s"><v>6</v></c>'
                '<c r="B2" t="s"><v>7</v></c>'
                '<c r="C2" t="s"><v>8</v></c>'
                '<c r="D2" t="s"><v>9</v></c>'
                '<c r="E2" t="s"><v>10</v></c>'
                '<c r="F2" t="s"><v>11</v></c>'
                '</row>'
                '</sheetData>'
                '</worksheet>',
            )

        xlsx_path = messy_data_dir["dir"] / "telebirr_statement.xlsx"
        xlsx_path.write_bytes(xlsx_buf.getvalue())

        out_html = tmp_path / "multi_stmt_report.html"
        result = run_health_check(
            invoices_path=messy_data_dir["invoices"],
            statements_paths=[messy_data_dir["statements"][0], xlsx_path],
            out_path=out_html,
            as_of_date=date(2026, 9, 15),
            keep=False,
        )

        assert out_html.is_file()
        assert len(result["meta"]["source_files"]) == 3
        # Invoices had INV-001 (45k), INV-002 (12k), INV-005 (85k), INV-006 (30k)
        # Telebirr statement matched INV-005 (85,000 ETB)
        assert result["matching"]["auto_confirmed"]["count"] >= 2

    def test_health_check_file_not_found(self, tmp_path):
        """Verify FileNotFoundError is raised for non-existent files."""
        missing = tmp_path / "nonexistent.csv"
        valid = tmp_path / "valid.csv"
        valid.write_text("Invoice No,Customer,Amount\nINV-1,A,100\n")

        with pytest.raises(FileNotFoundError):
            run_health_check(invoices_path=missing, statements_paths=[valid])

        with pytest.raises(FileNotFoundError):
            run_health_check(invoices_path=valid, statements_paths=[missing])

        with pytest.raises(FileNotFoundError):
            run_health_check(invoices_path=valid, statements_paths=[valid], map_path=missing)

    def test_cli_error_handling(self, tmp_path, monkeypatch, capsys):
        """Verify CLI gracefully outputs friendly error and exits with code 1."""
        bad_inv = str(tmp_path / "bad.csv")
        Path(bad_inv).write_text("Foo,Bar,Baz\n1,2,3\n")
        valid_stmt = str(tmp_path / "stmt.csv")
        Path(valid_stmt).write_text("Date,Amount\n2026-08-01,100\n")

        args = [
            "health-check",
            "--invoices", bad_inv,
            "--statements", valid_stmt,
        ]
        monkeypatch.setattr("sys.argv", ["app.cli"] + args)
        with pytest.raises(SystemExit) as exc:
            main()

        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "unrecognized column headers" in captured.err
