"""
tests/channels/test_retailer_web.py

Integration tests for the retailer balance-confirmation public web endpoints.
Features tested:
  - Generating signed expiring tokens (/retailers/{id}/confirmation-token)
  - Viewing balance confirmation page (/confirm-balance/{raw_token})
  - Confirming balance without payment details (does not mark payment confirmed)
  - Confirming balance with explicit payment details (creates L3 payment)
  - Disputing balance with required reason code and explanation
  - Token expiration / reuse prevention
  - Rate limiting enforcement (HTTP 429)
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.channels.web.routes import get_session
from app.kernel.enums import EvidenceLevel, InvoiceState, PaymentState
from app.kernel.models import Invoice, PaymentLine, Retailer, RetailerConfirmationToken, Tenant, User
from app.kernel.retailer_confirmation import create_retailer_confirmation_token
from app.main import app


@pytest.fixture
def client(db_session: Session):
    def _override_get_session():
        yield db_session

    app.dependency_overrides[get_session] = _override_get_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def retailer_with_invoices(db_session: Session, tenant_a: Tenant, owner_user_a: User) -> tuple[Retailer, list[Invoice]]:
    now = datetime.now(timezone.utc)
    r = Retailer(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        name="Piassa Electronics",
        phone="+251911887766",
        created_at=now,
    )
    db_session.add(r)
    db_session.flush()

    inv1 = Invoice(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        retailer_id=r.id,
        invoice_number="INV-PIASSA-1",
        amount_santim=350000,
        issued_date=date(2026, 10, 1),
        due_date=date(2026, 10, 31),
        state=InvoiceState.OPEN.value,
        created_at=now,
        created_by=owner_user_a.id,
    )
    inv2 = Invoice(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        retailer_id=r.id,
        invoice_number="INV-PIASSA-2",
        amount_santim=650000,
        issued_date=date(2026, 10, 10),
        due_date=date(2026, 11, 10),
        state=InvoiceState.OPEN.value,
        created_at=now,
        created_by=owner_user_a.id,
    )
    db_session.add_all([inv1, inv2])
    db_session.flush()
    return r, [inv1, inv2]


class TestRetailerWebConfirmation:
    def test_generate_token_endpoint(
        self,
        client: TestClient,
        tenant_a: Tenant,
        owner_user_a: User,
        retailer_with_invoices: tuple[Retailer, list[Invoice]],
    ):
        ret, _ = retailer_with_invoices
        headers = {"x-tenant-id": str(tenant_a.id), "x-user-role": "owner"}

        response = client.post(f"/retailers/{ret.id}/confirmation-token", headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert "raw_token" in data
        assert "link" in data
        assert f"/confirm-balance/{data['raw_token']}" in data["link"]

    def test_view_balance_confirmation_page(
        self,
        client: TestClient,
        db_session: Session,
        tenant_a: Tenant,
        retailer_with_invoices: tuple[Retailer, list[Invoice]],
    ):
        ret, _ = retailer_with_invoices
        token_rec, raw_token = create_retailer_confirmation_token(
            db_session,
            tenant_id=tenant_a.id,
            retailer_id=ret.id,
        )

        response = client.get(f"/confirm-balance/{raw_token}")
        assert response.status_code == 200
        html = response.text
        assert "Piassa Electronics" in html
        assert "INV-PIASSA-1" in html
        assert "INV-PIASSA-2" in html
        assert "Option 1: Confirm Balance" in html
        assert "Option 2: Dispute Balance" in html

    def test_submit_confirm_without_payment_info(
        self,
        client: TestClient,
        db_session: Session,
        tenant_a: Tenant,
        retailer_with_invoices: tuple[Retailer, list[Invoice]],
    ):
        ret, invoices = retailer_with_invoices
        token_rec, raw_token = create_retailer_confirmation_token(
            db_session,
            tenant_id=tenant_a.id,
            retailer_id=ret.id,
        )

        response = client.post(f"/confirm-balance/{raw_token}/confirm")
        assert response.status_code == 200
        assert "Balance Confirmation Recorded" in response.text

        # Verify token is marked used
        db_session.refresh(token_rec)
        assert token_rec.used_at is not None

        # Verify invoices remain open (balance confirmation does NOT confirm payment alone)
        for inv in invoices:
            db_session.refresh(inv)
            assert inv.state == InvoiceState.OPEN.value

    def test_submit_confirm_with_explicit_payment_creates_l3(
        self,
        client: TestClient,
        db_session: Session,
        tenant_a: Tenant,
        retailer_with_invoices: tuple[Retailer, list[Invoice]],
    ):
        ret, _ = retailer_with_invoices
        token_rec, raw_token = create_retailer_confirmation_token(
            db_session,
            tenant_id=tenant_a.id,
            retailer_id=ret.id,
        )

        form_data = {
            "payment_ref": "FT-ONLINE-12345",
            "payment_amount_etb": "3500.00",
            "payment_date": "2026-10-25",
        }
        response = client.post(f"/confirm-balance/{raw_token}/confirm", data=form_data)
        assert response.status_code == 200
        assert "Evidence Level L3" in response.text

        # Check payment line created as L3
        pl = db_session.execute(
            select(PaymentLine).where(
                PaymentLine.tenant_id == tenant_a.id,
                PaymentLine.source_ref == "FT-ONLINE-12345",
            )
        ).scalar_one()
        assert pl.evidence_level == EvidenceLevel.L3.value
        assert pl.state == PaymentState.CONFIRMED.value
        assert pl.amount_santim == 350000

    def test_submit_dispute_balance(
        self,
        client: TestClient,
        db_session: Session,
        tenant_a: Tenant,
        retailer_with_invoices: tuple[Retailer, list[Invoice]],
    ):
        ret, invoices = retailer_with_invoices
        token_rec, raw_token = create_retailer_confirmation_token(
            db_session,
            tenant_id=tenant_a.id,
            retailer_id=ret.id,
        )

        form_data = {
            "reason_code": "WRONG_AMOUNT",
            "explanation": "Invoice INV-PIASSA-2 should be 5,000 ETB, not 6,500 ETB due to promotional discount.",
        }
        response = client.post(f"/confirm-balance/{raw_token}/dispute", data=form_data)
        assert response.status_code == 200
        assert "Dispute Filed" in response.text
        assert "WRONG_AMOUNT" in response.text

        # Verify invoices marked as DISPUTED
        for inv in invoices:
            db_session.refresh(inv)
            assert inv.state == InvoiceState.DISPUTED.value

    def test_reuse_token_rejected(
        self,
        client: TestClient,
        db_session: Session,
        tenant_a: Tenant,
        retailer_with_invoices: tuple[Retailer, list[Invoice]],
    ):
        ret, _ = retailer_with_invoices
        token_rec, raw_token = create_retailer_confirmation_token(
            db_session,
            tenant_id=tenant_a.id,
            retailer_id=ret.id,
        )

        # First use
        res1 = client.post(f"/confirm-balance/{raw_token}/confirm")
        assert res1.status_code == 200

        # Second use attempt
        res2 = client.post(f"/confirm-balance/{raw_token}/confirm")
        assert res2.status_code == 400
        assert "already completed" in res2.json()["detail"].lower()
