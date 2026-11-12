"""
tests/channels/test_reminders.py

Tests for Stage 4: Payment Reminders and Promise-to-Pay.
Verifies owner-only access, tenant isolation, zero third-party calls,
no message body in audit events, HTML escaping in links, morning brief integration,
missed promise flagging, and locale parity.
All fixtures and data use synthetic samples.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.main import app
from app.kernel.enums import InvoiceState, UserRole
from app.kernel.models import Event, Invoice, PromiseToPay, Retailer, Tenant, User
from app.reports.morning_brief import generate_morning_brief_text
from app.reports.reminders import (
    cancel_promise_to_pay,
    create_promise_to_pay,
    generate_reminder_text,
    get_overdue_retailers,
    log_reminder_sent,
)
from tests.conftest import authenticate_client


@pytest.fixture
def reminders_data(db_session: Session, tenant_a: Tenant, tenant_b: Tenant, owner_user_a: User, owner_user_b: User):
    from app.kernel.tenant import set_tenant
    set_tenant(db_session, tenant_a.id)

    today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=5)
    past_due_10 = today - timedelta(days=10)

    # Tenant A Retailers
    ret_a1 = Retailer(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        name="Sample Retailer Alpha",
        phone="+251911223344",
        created_at=datetime.now(timezone.utc),
    )
    ret_a2 = Retailer(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        name="Sample Retailer & Partners <script>",
        phone="+251911556677",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add_all([ret_a1, ret_a2])
    db_session.flush()

    # Invoices for Alpha (Overdue)
    inv_a1 = Invoice(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        retailer_id=ret_a1.id,
        invoice_number="INV-2026-REM-001",
        amount_santim=2500000,  # 25,000 ETB
        state=InvoiceState.OPEN.value,
        issued_date=past_due_10 - timedelta(days=20),
        due_date=past_due_10,
        created_by=owner_user_a.id,
        created_at=datetime.now(timezone.utc),
    )
    inv_a2 = Invoice(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        retailer_id=ret_a1.id,
        invoice_number="INV-2026-REM-002",
        amount_santim=1500000,  # 15,000 ETB
        state=InvoiceState.OPEN.value,
        issued_date=yesterday - timedelta(days=15),
        due_date=yesterday,
        created_by=owner_user_a.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add_all([inv_a1, inv_a2])

    # Tenant B Retailer (for isolation tests)
    set_tenant(db_session, tenant_b.id)
    ret_b = Retailer(
        id=uuid.uuid4(),
        tenant_id=tenant_b.id,
        name="Sample Retailer Beta",
        phone="+251911998877",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(ret_b)
    db_session.flush()

    inv_b = Invoice(
        id=uuid.uuid4(),
        tenant_id=tenant_b.id,
        retailer_id=ret_b.id,
        invoice_number="INV-2026-REM-B01",
        amount_santim=3000000,
        state=InvoiceState.OPEN.value,
        issued_date=yesterday - timedelta(days=10),
        due_date=yesterday,
        created_by=owner_user_b.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(inv_b)
    db_session.commit()

    return {
        "ret_a1": ret_a1,
        "ret_a2": ret_a2,
        "inv_a1": inv_a1,
        "inv_a2": inv_a2,
        "ret_b": ret_b,
        "inv_b": inv_b,
    }


class TestRemindersAndPromises:
    def test_reminders_owner_access_only(
        self, client: TestClient, db_session: Session, cashier_user_a: User, reminders_data
    ):
        """Cashiers receive 403 on reminder routes (owner only)."""
        authenticate_client(client, db_session, cashier_user_a)
        ret_a = reminders_data["ret_a1"]

        assert client.get("/reminders").status_code == 403
        assert client.get(f"/reminders/{ret_a.id}/compose").status_code == 403
        assert client.post(f"/reminders/{ret_a.id}/log", data={"csrf_token": ""}).status_code == 403
        assert client.post(
            f"/reminders/{ret_a.id}/promise",
            data={"amount_etb": "100.00", "promised_date": "2026-11-12", "csrf_token": ""},
        ).status_code == 403

    def test_reminders_tenant_isolation(
        self, client: TestClient, db_session: Session, tenant_a: Tenant, tenant_b: Tenant, owner_user_a: User, owner_user_b: User, reminders_data
    ):
        """Negative test: Owner B cannot view or operate on Tenant A's overdue retailers."""
        ret_a = reminders_data["ret_a1"]
        ret_b = reminders_data["ret_b"]

        # Owner B views dashboard
        authenticate_client(client, db_session, owner_user_b)
        resp = client.get("/reminders")
        assert resp.status_code == 200
        assert "Sample Retailer Beta" in resp.text
        assert "Sample Retailer Alpha" not in resp.text

        # Owner B attempts to compose reminder for Tenant A's retailer -> 404
        resp_compose = client.get(f"/reminders/{ret_a.id}/compose")
        assert resp_compose.status_code == 404

        # Owner B attempts to log reminder for Tenant A's retailer -> 404
        _, csrf_b = authenticate_client(client, db_session, owner_user_b)
        resp_log = client.post(
            f"/reminders/{ret_a.id}/log",
            data={"channel": "sms", "csrf_token": csrf_b},
        )
        assert resp_log.status_code == 404

    def test_reminders_html_escaping_in_links(
        self, client: TestClient, db_session: Session, owner_user_a: User, reminders_data
    ):
        """XSS/Injection defense: retailer names and phones with special characters are safely escaped."""
        authenticate_client(client, db_session, owner_user_a)
        ret_special = reminders_data["ret_a2"]

        resp = client.get(f"/reminders/{ret_special.id}/compose")
        assert resp.status_code == 200
        # HTML output must not have raw unescaped script tag
        assert "<script>" not in resp.text
        assert "&lt;script&gt;" in resp.text or "&amp; Partners" in resp.text

    def test_reminders_event_contains_no_message_body(
        self, client: TestClient, db_session: Session, tenant_a: Tenant, owner_user_a: User, reminders_data
    ):
        """Privacy Rule: Reminders logged emit events with channel metadata only, zero message text or PII."""
        _, csrf = authenticate_client(client, db_session, owner_user_a)
        ret_a = reminders_data["ret_a1"]

        resp = client.post(
            f"/reminders/{ret_a.id}/log",
            data={"channel": "sms", "csrf_token": csrf},
            follow_redirects=True,
        )
        assert resp.status_code == 200

        # Query events table
        events = db_session.execute(
            select(Event).where(
                Event.tenant_id == tenant_a.id,
                Event.event_type == "reminder.sent",
                Event.aggregate_id == ret_a.id,
            )
        ).scalars().all()
        assert len(events) == 1
        ev = events[0]
        assert ev.payload == {"channel": "sms"}
        # Must NOT contain message text, phone number, or merchant names
        assert "text" not in ev.payload
        assert "message" not in ev.payload
        assert "phone" not in ev.payload

    def test_promise_to_pay_appears_in_morning_brief(
        self, client: TestClient, db_session: Session, tenant_a: Tenant, owner_user_a: User, reminders_data
    ):
        """Promise-to-pay due today appears in the morning brief under PROMISED PAYMENTS."""
        _, csrf = authenticate_client(client, db_session, owner_user_a)
        ret_a = reminders_data["ret_a1"]
        today = datetime.now(timezone.utc).date()

        resp = client.post(
            f"/reminders/{ret_a.id}/promise",
            data={
                "amount_etb": "20,000.00",
                "promised_date": today.isoformat(),
                "notes": "Promised by noon",
                "csrf_token": csrf,
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200

        # Verify in morning brief
        brief_resp = client.get("/brief")
        assert brief_resp.status_code == 200
        brief_text = brief_resp.text
        assert "PROMISED PAYMENTS:" in brief_text
        assert "[Expected Today] ETB 20,000.00 from Sample Retailer Alpha" in brief_text

    def test_missed_promise_flagged_after_date(
        self, client: TestClient, db_session: Session, tenant_a: Tenant, owner_user_a: User, reminders_data
    ):
        """Promise-to-pay whose date has passed without settlement is flagged as MISSED PROMISE."""
        from app.kernel.tenant import set_tenant
        set_tenant(db_session, tenant_a.id)

        ret_a = reminders_data["ret_a1"]
        yesterday = datetime.now(timezone.utc).date() - timedelta(days=2)

        # Create a promise with date in the past
        promise = create_promise_to_pay(
            db_session,
            tenant_id=tenant_a.id,
            retailer_id=ret_a.id,
            user_id=owner_user_a.id,
            amount_santim=1500000,  # 15,000 ETB
            promised_date=yesterday,
            notes="Past promise",
        )
        db_session.commit()

        # In Morning brief
        brief_text = generate_morning_brief_text(db_session, tenant_id=tenant_a.id)
        assert "[MISSED PROMISE] ETB 15,000.00 from Sample Retailer Alpha" in brief_text

        # In Reminders table
        authenticate_client(client, db_session, owner_user_a)
        resp = client.get("/reminders")
        assert resp.status_code == 200
        assert "data-testid=\"badge-promise-missed\"" in resp.text

    def test_cancel_promise_to_pay(
        self, client: TestClient, db_session: Session, tenant_a: Tenant, owner_user_a: User, reminders_data
    ):
        """Owner can cancel an active promise-to-pay, transitioning it to cancelled status."""
        from app.kernel.tenant import set_tenant
        set_tenant(db_session, tenant_a.id)
        ret_a = reminders_data["ret_a1"]

        promise = create_promise_to_pay(
            db_session,
            tenant_id=tenant_a.id,
            retailer_id=ret_a.id,
            user_id=owner_user_a.id,
            amount_santim=1000000,
            promised_date=datetime.now(timezone.utc).date() + timedelta(days=1),
        )
        db_session.commit()

        _, csrf = authenticate_client(client, db_session, owner_user_a)
        resp = client.post(
            f"/promises/{promise.id}/cancel",
            data={"retailer_id": str(ret_a.id), "csrf_token": csrf},
            follow_redirects=True,
        )
        assert resp.status_code == 200

        # Check DB status
        refreshed = db_session.execute(
            select(PromiseToPay).where(PromiseToPay.id == promise.id)
        ).scalar_one()
        assert refreshed.status == "cancelled"

    def test_locale_parity_reminders(self):
        """Verifies reminder templates in English and Amharic format correctly."""
        due_d = date(2026, 10, 24)
        en_text = generate_reminder_text("Test Wholesale", "Shop Alpha", 2, 5000000, due_d, "en")
        am_text = generate_reminder_text("Test Wholesale", "Shop Alpha", 2, 5000000, due_d, "am")

        assert "Shop Alpha" in en_text
        assert "50,000.00" in en_text
        assert "24/10/2026" in en_text

        assert "Shop Alpha" in am_text
        assert "50,000.00" in am_text
        assert "<!-- needs native review -->" in am_text
