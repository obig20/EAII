"""
tests/adversarial/test_file_handling_attacks.py

Task 7: File Handling & Parser Hardening:
1. Null bytes in CSV
2. Malformed non-UTF-8 encodings
3. Truncated XLSX / corrupted zip headers
4. Billion Laughs XML entity expansion in XLSX
"""

from __future__ import annotations

import io
import zipfile
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.importers.xlsx_importer import _parse_xlsx_pure_python
from tests.conftest import authenticate_client


class TestFileHandlingAttacks:
    @pytest.mark.xfail(strict=True, reason="SEC-VULN-13, Stage B")
    def test_null_bytes_in_csv_upload(
        self, client: TestClient, db_session: Session, owner_user_a
    ):
        """CSV containing embedded null bytes must be rejected or stripped without crash."""
        authenticate_client(client, db_session, owner_user_a)
        csv_with_null = b"Invoice Number,Retailer,Amount,Due Date\nINV-001\x00HACK,Abebe,100.00,2026-12-31"
        files = {"file": ("sales_null.csv", csv_with_null, "text/csv")}
        res = client.post("/sales/bulk", files=files)
        assert res.status_code in (400, 422)

    def test_non_utf8_encoding_in_csv_upload(
        self, client: TestClient, db_session: Session, owner_user_a
    ):
        """CSV with invalid UTF-8 (e.g. raw Windows-1252 bytes) is rejected cleanly."""
        authenticate_client(client, db_session, owner_user_a)
        cp1252_data = "Invoice Number,Retailer,Amount,Due Date\nINV-002,Café,100.00,2026-12-31".encode("cp1252")
        files = {"file": ("sales_cp1252.csv", cp1252_data, "text/csv")}
        res = client.post("/sales/bulk", files=files)
        # Should either parse cleanly or return 400 error (never 500)
        assert res.status_code in (200, 303, 400)

    def test_truncated_xlsx_file_rejected(self):
        """Truncated XLSX byte stream raises BadZipFile or ValueError."""
        truncated_bytes = b"PK\x03\x04" + b"\x00" * 50
        with pytest.raises(Exception):
            _parse_xlsx_pure_python(truncated_bytes)

    @pytest.mark.xfail(strict=True, reason="SEC-VULN-13, Stage B")
    def test_billion_laughs_xml_in_xlsx_blocked(self):
        """XML entity expansion in sheet1.xml must be blocked by parser defusedxml / entity refusal."""
        xml_bomb = (
            '<?xml version="1.0"?>\n'
            '<!DOCTYPE lolz [\n'
            ' <!ENTITY lol "lol">\n'
            ' <!ELEMENT lolz (#PCDATA)>\n'
            ' <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">\n'
            ']>\n'
            '<worksheet><sheetData><row r="1"><c r="A1" t="s"><v>&lol1;</v></c></row></sheetData></worksheet>'
        )
        bio = io.BytesIO()
        with zipfile.ZipFile(bio, "w", compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr("xl/worksheets/sheet1.xml", xml_bomb.encode("utf-8"))
            z.writestr("[Content_Types].xml", b"<Types></Types>")

        bio.seek(0)
        with pytest.raises(Exception):
            _parse_xlsx_pure_python(bio.getvalue())
