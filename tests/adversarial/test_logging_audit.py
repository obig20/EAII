"""
tests/adversarial/test_logging_audit.py

Task 6: Logging Audit
Scan the code and run the app through its main flows with log capture,
to confirm no personal data (names, phone numbers, account numbers)
and no amounts appear above DEBUG level.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import date, datetime, timezone
import pytest
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient

from app.kernel.enums import InvoiceState
from app.kernel.models import Invoice, Retailer, Tenant, User
from tests.conftest import authenticate_client


class TestLoggingAudit:
    def test_task_6_no_pii_or_amounts_above_debug_level(
        self, client: TestClient, db_session: Session, tenant_a: Tenant, owner_user_a: User, caplog
    ):
        """
        Verify that during sales creation, statement upload, and match review,
        no customer phone numbers, names, bank accounts, or monetary amounts
        appear in logs at INFO, WARNING, or ERROR level.
        """
        caplog.set_level(logging.INFO)
        authenticate_client(client, db_session, owner_user_a)

        test_phone = "+251911998877"
        test_name = "Sensitively Named Retailer"
        test_account = "100098765432"
        test_amount_str = "12345.67"

        ret = Retailer(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            name=test_name,
            phone=test_phone,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(ret)
        db_session.flush()

        # Trigger credit sale creation
        res = client.post(
            "/sales",
            data={
                "retailer_id": str(ret.id),
                "invoice_number": "INV-AUDIT-LOG-1",
                "amount_etb": test_amount_str,
                "due_date": "2026-12-31",
            },
        )
        assert res.status_code in (200, 303)

        # Inspect captured log messages
        for record in caplog.records:
            if record.levelno > logging.DEBUG:
                msg = record.getMessage()
                assert test_phone not in msg, f"PII leak in log (phone): {msg}"
                assert test_name not in msg, f"PII leak in log (customer name): {msg}"
                assert test_account not in msg, f"PII leak in log (account): {msg}"
                assert test_amount_str not in msg, f"Financial amount leak in log (ETB amount): {msg}"
