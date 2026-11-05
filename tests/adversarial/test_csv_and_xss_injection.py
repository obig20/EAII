"""
tests/adversarial/test_csv_and_xss_injection.py

Audit for:
1. CSV Formula Injection (CWE-1236) across exports (sales.csv, payments.csv, accountant.csv)
2. Stored XSS & Telegram HTML Injection in bot messages (handle_review) and HTMX partials
"""

from __future__ import annotations

import html
import uuid
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock
import pytest
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient

from app.channels.telegram.bot import handle_review
from app.kernel.enums import EvidenceLevel, InvoiceState, MatchState, PaymentState
from app.kernel.models import Invoice, PaymentLine, PaymentMatch, Retailer, Tenant, User
from app.reports.exports import export_accountant_csv, export_payments_csv, export_sales_csv
from tests.conftest import authenticate_client


class TestCsvAndInjectionAttacks:
    def test_csv_formula_injection_in_sales_export(
        self, db_session: Session, tenant_a: Tenant, owner_user_a: User
    ):
        """
        CWE-1236: Retailer name or invoice number starting with =, +, -, @
        must be sanitized (e.g. prepended with single-quote or escaped)
        so that spreadsheet applications (Excel, LibreOffice) do not execute formulas.
        """
        payloads = [
            "=cmd|' /C calc'!A0",
            "+10+20",
            "-SUM(A1:A10)",
            "@HYPERLINK('http://attacker.com','Click Me')",
        ]

        for i, payload in enumerate(payloads):
            ret = Retailer(
                id=uuid.uuid4(),
                tenant_id=tenant_a.id,
                name=payload,  # Cell starts directly with =, +, -, @
                phone=f"+25191199000{i}",
                created_at=datetime.now(timezone.utc),
            )
            inv = Invoice(
                id=uuid.uuid4(),
                tenant_id=tenant_a.id,
                retailer_id=ret.id,
                invoice_number=f"INV-FORMULA-{i}",
                amount_santim=10000,
                issued_date=date(2026, 1, 1),
                due_date=date(2026, 1, 15),
                state=InvoiceState.OPEN.value,
                created_at=datetime.now(timezone.utc),
                created_by=owner_user_a.id,
            )
            db_session.add_all([ret, inv])
        db_session.flush()

        csv_content = export_sales_csv(db_session, tenant_id=tenant_a.id)

        # Check each line: Any cell starting with =, +, -, @ is vulnerable if not sanitized
        for line in csv_content.strip().splitlines()[1:]:
            cells = line.split(",")
            for cell in cells:
                clean_cell = cell.strip('"').strip()
                is_valid_phone = clean_cell.startswith("+") and clean_cell[1:].isdigit()
                # A sanitized cell should prepend a single quote (e.g. "'=")
                assert not (clean_cell.startswith("=") or clean_cell.startswith("@") or (clean_cell.startswith("+") and not is_valid_phone) or (clean_cell.startswith("-") and not clean_cell[1:].isdigit())), (
                    f"VULNERABILITY SEC-VULN-03 CONFIRMED: Formula injection detected! "
                    f"Cell starts with unsanitized formula trigger: {clean_cell}"
                )

    def test_csv_phone_numbers_survive_and_formulas_neutralized(
        self, db_session: Session, tenant_a: Tenant, owner_user_a: User
    ):
        """
        SEC-03: Ensure phone numbers (e.g. +251911000000) survive export unchanged
        without being prefixed with single-quote ('), while formula injections
        like =cmd|'/c calc'!A0 are neutralized.
        """
        import csv as pycsv

        ret = Retailer(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            name="=cmd|'/c calc'!A0",
            phone="+251911000000",
            created_at=datetime.now(timezone.utc),
        )
        inv = Invoice(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            retailer_id=ret.id,
            invoice_number="INV-SAFE-PHONE",
            amount_santim=10000,
            issued_date=date(2026, 1, 1),
            due_date=date(2026, 1, 15),
            state=InvoiceState.OPEN.value,
            created_at=datetime.now(timezone.utc),
            created_by=owner_user_a.id,
        )
        db_session.add_all([ret, inv])
        db_session.flush()

        csv_content = export_sales_csv(db_session, tenant_id=tenant_a.id)
        reader = list(pycsv.reader(csv_content.strip().splitlines()))
        row = next(r for r in reader[1:] if r[0] == "INV-SAFE-PHONE")
        assert row[1] == "'=cmd|'/c calc'!A0"
        assert row[2] == "+251911000000"

    def test_telegram_html_injection_in_handle_review(
        self, db_session: Session, tenant_a: Tenant, owner_user_a: User
    ):
        """
        Stored XSS / Telegram HTML Injection:
        Retailer name containing unescaped HTML tags (e.g. '<b onmouseover=...>' or unclosed '<b>')
        must be HTML-escaped before being interpolated into Telegram messages with parse_mode='HTML'.
        """
        malicious_name = "<script>alert('xss')</script> & <b>Unclosed Bold"
        ret = Retailer(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            name=malicious_name,
            phone="+251911999888",
            created_at=datetime.now(timezone.utc),
        )
        inv = Invoice(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            retailer_id=ret.id,
            invoice_number="INV-XSS-01",
            amount_santim=50000,
            issued_date=date(2026, 1, 1),
            due_date=date(2026, 1, 15),
            state=InvoiceState.OPEN.value,
            created_at=datetime.now(timezone.utc),
            created_by=owner_user_a.id,
        )
        pl = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            amount_santim=50000,
            value_date=date(2026, 1, 2),
            counterparty_name="<img src=x onerror=alert(1)>",
            raw_reference="<a href='http://evil.com'>Click</a>",
            evidence_level=EvidenceLevel.L2.value,
            state=PaymentState.PROPOSED.value,
            created_at=datetime.now(timezone.utc),
        )
        pm = PaymentMatch(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            invoice_id=inv.id,
            payment_line_id=pl.id,
            matched_santim=50000,
            tier="tier_b",
            state=MatchState.PROPOSED.value,
            created_at=datetime.now(timezone.utc),
        )
        owner_user_a.telegram_id = 777111
        db_session.add_all([ret, inv, pl, pm])
        db_session.flush()

        # Mock telegram message and bot
        msg = MagicMock()
        msg.from_user.id = 777111
        msg.answer = AsyncMock()

        from unittest.mock import patch
        import asyncio

        with patch("app.channels.telegram.bot.get_session_scope") as mock_scope:
            mock_scope.return_value.__enter__.return_value = db_session
            asyncio.run(handle_review(msg))

        assert msg.answer.called, "Telegram handle_review did not answer"
        call_args = msg.answer.call_args
        sent_text = call_args[0][0]
        # Verify that malicious raw HTML tags were escaped
        assert "<script>" not in sent_text, (
            f"VULNERABILITY SEC-VULN-04 CONFIRMED: Unescaped raw HTML tag found in Telegram message: {sent_text}"
        )
        assert html.escape(malicious_name) in sent_text or "&lt;script&gt;" in sent_text, (
            "Telegram message failed to HTML-escape retailer name!"
        )
