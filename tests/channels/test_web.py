"""
tests/channels/test_web.py

Tests for Daily Close web routes and daily close 3-step workflow.
Includes:
  - Step 1: Credit-sale capture web form & CSV bulk upload
  - Step 2: Statement upload & auto-matching
  - Step 3: Match approval, L4 rejection / owner unverified override, and rejection
  - Plain-text Morning collections brief
  - Full CSV exports & partner accountant export
  - Security & Guard tests:
      * Rejecting duplicate invoice numbers (Rule 5)
      * L4 evidence CANNOT be approved as confirmed (Rule 2)
      * Cashier cannot mark L4 accepted_unverified (Owner only)
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.channels.web.routes import get_session
from app.kernel.enums import EvidenceLevel, InvoiceState, MatchState, PaymentState, UserRole
from app.kernel.models import ImportBatch, Invoice, PaymentLine, PaymentMatch, Retailer, Tenant, User
from app.kernel.tenant import set_tenant
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
def web_tenant(db_session: Session) -> Tenant:
    t = Tenant(id=uuid.uuid4(), name="Web Test Tenant", created_at=datetime.now(timezone.utc))
    db_session.add(t)
    db_session.flush()
    return t


@pytest.fixture
def web_owner(db_session: Session, web_tenant: Tenant) -> User:
    u = User(
        id=uuid.uuid4(),
        tenant_id=web_tenant.id,
        role=UserRole.OWNER.value,
        display_name="Web Owner",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(u)
    db_session.flush()
    return u


@pytest.fixture
def web_cashier(db_session: Session, web_tenant: Tenant) -> User:
    u = User(
        id=uuid.uuid4(),
        tenant_id=web_tenant.id,
        role=UserRole.CASHIER.value,
        display_name="Web Cashier",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(u)
    db_session.flush()
    return u


@pytest.fixture
def web_retailer(db_session: Session, web_tenant: Tenant) -> Retailer:
    r = Retailer(
        id=uuid.uuid4(),
        tenant_id=web_tenant.id,
        name="Addis Wholesale Market",
        phone="+251911334455",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(r)
    db_session.flush()
    return r


class TestWebDashboard:
    def test_dashboard_renders(
        self,
        client: TestClient,
        web_tenant: Tenant,
        web_owner: User,
    ):
        headers = {"x-tenant-id": str(web_tenant.id), "x-user-role": "owner"}
        response = client.get("/", headers=headers)
        assert response.status_code == 200
        html = response.text
        assert "Daily Close" in html
        assert "Record Credit Sale" in html
        assert "Import Today's Statement" in html
        assert "Approve Proposed Matches" in html
        assert "Accounts Receivable Aging" in html


class TestWebCreditSales:
    def test_create_credit_sale_success(
        self,
        client: TestClient,
        db_session: Session,
        web_tenant: Tenant,
        web_owner: User,
        web_retailer: Retailer,
    ):
        headers = {"x-tenant-id": str(web_tenant.id), "x-user-role": "owner"}
        data = {
            "invoice_number": "INV-WEB-001",
            "retailer_id": str(web_retailer.id),
            "amount_etb": "1500.50",
            "due_date": "2026-11-30",
        }
        response = client.post("/sales", data=data, headers=headers, follow_redirects=False)
        assert response.status_code == 303
        assert "msg=" in response.headers["location"]

        # Verify DB entry
        inv = db_session.execute(
            select(Invoice).where(
                Invoice.tenant_id == web_tenant.id,
                Invoice.invoice_number == "INV-WEB-001",
            )
        ).scalar_one()
        assert inv.amount_santim == 150050
        assert inv.retailer_id == web_retailer.id

    def test_create_credit_sale_rejects_duplicate(
        self,
        client: TestClient,
        web_tenant: Tenant,
        web_owner: User,
        web_retailer: Retailer,
    ):
        headers = {"x-tenant-id": str(web_tenant.id), "x-user-role": "owner"}
        data = {
            "invoice_number": "INV-DUP-WEB",
            "retailer_id": str(web_retailer.id),
            "amount_etb": "2000.00",
            "due_date": "2026-11-30",
        }
        res1 = client.post("/sales", data=data, headers=headers)
        assert res1.status_code == 200 or res1.status_code == 303

        # Post duplicate invoice number
        res2 = client.post("/sales", data=data, headers=headers)
        assert res2.status_code == 400
        assert "already exists" in res2.json()["detail"]

    def test_create_credit_sale_invalid_parameters(
        self,
        client: TestClient,
        web_tenant: Tenant,
        web_owner: User,
        web_retailer: Retailer,
    ):
        headers = {"x-tenant-id": str(web_tenant.id), "x-user-role": "owner"}
        # Invalid amount
        data_bad_amt = {
            "invoice_number": "INV-BAD-AMT",
            "retailer_id": str(web_retailer.id),
            "amount_etb": "-500.00",
            "due_date": "2026-11-30",
        }
        res = client.post("/sales", data=data_bad_amt, headers=headers)
        assert res.status_code == 400

    def test_bulk_sales_csv_upload(
        self,
        client: TestClient,
        db_session: Session,
        web_tenant: Tenant,
        web_owner: User,
    ):
        headers = {"x-tenant-id": str(web_tenant.id), "x-user-role": "owner"}
        csv_content = (
            "Invoice Number,Retailer Name,Amount,Issue Date,Due Date,Phone\n"
            "INV-BULK-1,Bulker One,4000.00,2026-10-01,2026-10-31,+251911999888\n"
            "INV-BULK-2,Bulker Two,6500.00,2026-10-02,2026-11-01,+251911777666\n"
        )
        files = {"file": ("bulk_sales.csv", csv_content.encode("utf-8"), "text/csv")}
        response = client.post("/sales/bulk", files=files, headers=headers, follow_redirects=False)
        assert response.status_code == 303

        invoices = db_session.execute(
            select(Invoice).where(
                Invoice.tenant_id == web_tenant.id,
                Invoice.invoice_number.in_(["INV-BULK-1", "INV-BULK-2"]),
            )
        ).scalars().all()
        assert len(invoices) == 2


class TestWebStatementsAndMatching:
    def test_statement_upload_cbe_csv(
        self,
        client: TestClient,
        db_session: Session,
        web_tenant: Tenant,
        web_owner: User,
        web_retailer: Retailer,
    ):
        headers = {"x-tenant-id": str(web_tenant.id), "x-user-role": "owner"}

        # Seed an invoice to match
        now = datetime.now(timezone.utc)
        inv = Invoice(
            id=uuid.uuid4(),
            tenant_id=web_tenant.id,
            invoice_number="INV-CBE-100",
            retailer_id=web_retailer.id,
            amount_santim=500000,
            issued_date=date(2026, 10, 1),
            due_date=date(2026, 10, 31),
            state=InvoiceState.OPEN.value,
            created_at=now,
            created_by=web_owner.id,
        )
        db_session.add(inv)
        db_session.flush()

        # CBE CSV content containing INV-CBE-100
        csv_stmt = (
            "Posting Date,Credit Amount,Reference No,Description,Counterparty Name,Transaction ID\n"
            "20/10/2026,5000.00,INV-CBE-100,Payment for INV-CBE-100,Addis Wholesale Market,TX99887766\n"
        )
        files = {"file": ("cbe_statement.csv", csv_stmt.encode("utf-8"), "text/csv")}
        data = {"source_name": "cbe_csv"}

        response = client.post("/statements/upload", data=data, files=files, headers=headers, follow_redirects=False)
        assert response.status_code == 303

        # Verify payment line created and auto-matched (Tier A)
        pl = db_session.execute(
            select(PaymentLine).where(
                PaymentLine.tenant_id == web_tenant.id,
                PaymentLine.source_ref == "TX99887766",
            )
        ).scalar_one()
        assert pl.amount_santim == 500000
        assert pl.evidence_level == EvidenceLevel.L2.value
        assert pl.state == PaymentState.CONFIRMED.value


class TestWebMatchApprovalsAndGuards:
    def test_approve_proposed_l2_match_success(
        self,
        client: TestClient,
        db_session: Session,
        web_tenant: Tenant,
        web_owner: User,
        web_retailer: Retailer,
    ):
        headers = {"x-tenant-id": str(web_tenant.id), "x-user-role": "owner"}
        now = datetime.now(timezone.utc)

        inv = Invoice(
            id=uuid.uuid4(),
            tenant_id=web_tenant.id,
            invoice_number="INV-APP-1",
            retailer_id=web_retailer.id,
            amount_santim=300000,
            issued_date=date(2026, 10, 1),
            due_date=date(2026, 10, 31),
            state=InvoiceState.OPEN.value,
            created_at=now,
            created_by=web_owner.id,
        )
        batch = ImportBatch(
            id=uuid.uuid4(),
            tenant_id=web_tenant.id,
            source_type="bank_statement",
            evidence_level=EvidenceLevel.L2.value,
            filename="stmt.csv",
            imported_by=web_owner.id,
            imported_at=now,
        )
        pl = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=web_tenant.id,
            import_batch_id=batch.id,
            value_date=date(2026, 10, 15),
            amount_santim=300000,
            evidence_level=EvidenceLevel.L2.value,
            state=PaymentState.PROPOSED.value,
            created_at=now,
        )
        pm = PaymentMatch(
            id=uuid.uuid4(),
            tenant_id=web_tenant.id,
            payment_line_id=pl.id,
            invoice_id=inv.id,
            matched_santim=300000,
            tier="tier_b",
            state=MatchState.PROPOSED.value,
            created_at=now,
        )
        db_session.add_all([inv, batch, pl, pm])
        db_session.flush()

        # Approve L2 match
        res = client.post(f"/matches/{pm.id}/approve", headers=headers)
        assert res.status_code == 200
        assert "Match Confirmed" in res.text

        # Verify DB
        db_session.refresh(pm)
        db_session.refresh(pl)
        assert pm.state == MatchState.CONFIRMED.value
        assert pl.state == PaymentState.CONFIRMED.value

    def test_approve_proposed_l4_evidence_strictly_forbidden(
        self,
        client: TestClient,
        db_session: Session,
        web_tenant: Tenant,
        web_owner: User,
        web_retailer: Retailer,
    ):
        """
        NON-NEGOTIABLE RULE 2:
        L4 evidence (screenshot, SMS, manual note) can NEVER be confirmed!
        Attempting to approve an L4 match must return 400 Bad Request.
        """
        headers = {"x-tenant-id": str(web_tenant.id), "x-user-role": "owner"}
        now = datetime.now(timezone.utc)

        inv = Invoice(
            id=uuid.uuid4(),
            tenant_id=web_tenant.id,
            invoice_number="INV-L4-GUARD",
            retailer_id=web_retailer.id,
            amount_santim=450000,
            issued_date=date(2026, 10, 1),
            due_date=date(2026, 10, 31),
            state=InvoiceState.OPEN.value,
            created_at=now,
            created_by=web_owner.id,
        )
        batch = ImportBatch(
            id=uuid.uuid4(),
            tenant_id=web_tenant.id,
            source_type="manual_note",
            evidence_level=EvidenceLevel.L4.value,
            filename=None,
            imported_by=web_owner.id,
            imported_at=now,
        )
        pl = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=web_tenant.id,
            import_batch_id=batch.id,
            value_date=date(2026, 10, 15),
            amount_santim=450000,
            evidence_level=EvidenceLevel.L4.value,  # L4!
            state=PaymentState.PROPOSED.value,
            created_at=now,
        )
        pm = PaymentMatch(
            id=uuid.uuid4(),
            tenant_id=web_tenant.id,
            payment_line_id=pl.id,
            invoice_id=inv.id,
            matched_santim=450000,
            tier="tier_c",
            state=MatchState.PROPOSED.value,
            created_at=now,
        )
        db_session.add_all([inv, batch, pl, pm])
        db_session.flush()

        res = client.post(f"/matches/{pm.id}/approve", headers=headers)
        assert res.status_code == 400
        assert "cannot set a payment to 'confirmed'" in res.json()["detail"].lower()

        # Invariant check: PM and PL must NOT be confirmed!
        db_session.refresh(pm)
        db_session.refresh(pl)
        assert pm.state != MatchState.CONFIRMED.value
        assert pl.state != PaymentState.CONFIRMED.value

    def test_owner_can_mark_l4_as_accepted_unverified(
        self,
        client: TestClient,
        db_session: Session,
        web_tenant: Tenant,
        web_owner: User,
    ):
        headers = {"x-tenant-id": str(web_tenant.id), "x-user-role": "owner"}
        now = datetime.now(timezone.utc)

        batch = ImportBatch(
            id=uuid.uuid4(),
            tenant_id=web_tenant.id,
            source_type="screenshot",
            evidence_level=EvidenceLevel.L4.value,
            filename=None,
            imported_by=web_owner.id,
            imported_at=now,
        )
        pl = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=web_tenant.id,
            import_batch_id=batch.id,
            value_date=date(2026, 10, 15),
            amount_santim=120000,
            evidence_level=EvidenceLevel.L4.value,
            state=PaymentState.PROPOSED.value,
            created_at=now,
        )
        db_session.add_all([batch, pl])
        db_session.flush()

        res = client.post(f"/matches/{pl.id}/mark-unverified", headers=headers)
        assert res.status_code == 200
        assert "Accepted Unverified" in res.text

        db_session.refresh(pl)
        assert pl.state == PaymentState.ACCEPTED_UNVERIFIED.value

    def test_cashier_cannot_mark_l4_as_accepted_unverified(
        self,
        client: TestClient,
        db_session: Session,
        web_tenant: Tenant,
        web_cashier: User,
    ):
        headers = {"x-tenant-id": str(web_tenant.id), "x-user-role": "cashier"}
        now = datetime.now(timezone.utc)

        batch = ImportBatch(
            id=uuid.uuid4(),
            tenant_id=web_tenant.id,
            source_type="screenshot",
            evidence_level=EvidenceLevel.L4.value,
            filename=None,
            imported_by=web_cashier.id,
            imported_at=now,
        )
        pl = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=web_tenant.id,
            import_batch_id=batch.id,
            value_date=date(2026, 10, 15),
            amount_santim=120000,
            evidence_level=EvidenceLevel.L4.value,
            state=PaymentState.PROPOSED.value,
            created_at=now,
        )
        db_session.add_all([batch, pl])
        db_session.flush()

        res = client.post(f"/matches/{pl.id}/mark-unverified", headers=headers)
        assert res.status_code == 403
        assert "Only owners can mark L4 items as accepted_unverified" in res.json()["detail"]

    def test_reject_match(
        self,
        client: TestClient,
        db_session: Session,
        web_tenant: Tenant,
        web_owner: User,
        web_retailer: Retailer,
    ):
        headers = {"x-tenant-id": str(web_tenant.id), "x-user-role": "owner"}
        now = datetime.now(timezone.utc)

        inv = Invoice(
            id=uuid.uuid4(),
            tenant_id=web_tenant.id,
            invoice_number="INV-REJ-1",
            retailer_id=web_retailer.id,
            amount_santim=100000,
            issued_date=date(2026, 10, 1),
            due_date=date(2026, 10, 31),
            state=InvoiceState.OPEN.value,
            created_at=now,
            created_by=web_owner.id,
        )
        batch = ImportBatch(
            id=uuid.uuid4(),
            tenant_id=web_tenant.id,
            source_type="bank_statement",
            evidence_level=EvidenceLevel.L2.value,
            filename=None,
            imported_by=web_owner.id,
            imported_at=now,
        )
        pl = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=web_tenant.id,
            import_batch_id=batch.id,
            value_date=date(2026, 10, 15),
            amount_santim=100000,
            evidence_level=EvidenceLevel.L2.value,
            state=PaymentState.PROPOSED.value,
            created_at=now,
        )
        pm = PaymentMatch(
            id=uuid.uuid4(),
            tenant_id=web_tenant.id,
            payment_line_id=pl.id,
            invoice_id=inv.id,
            matched_santim=100000,
            tier="tier_c",
            state=MatchState.PROPOSED.value,
            created_at=now,
        )
        db_session.add_all([inv, batch, pl, pm])
        db_session.flush()

        res = client.post(f"/matches/{pm.id}/reject", headers=headers)
        assert res.status_code == 200
        assert "Match Rejected" in res.text

        db_session.refresh(pm)
        db_session.refresh(pl)
        assert pm.state == MatchState.REJECTED.value
        assert pl.state == PaymentState.REJECTED.value


class TestWebReportsAndExports:
    def test_morning_brief_endpoint(
        self,
        client: TestClient,
        web_tenant: Tenant,
        web_owner: User,
    ):
        headers = {"x-tenant-id": str(web_tenant.id), "x-user-role": "owner"}
        res = client.get("/brief", headers=headers)
        assert res.status_code == 200
        assert "text/plain" in res.headers["content-type"]
        assert "DAILY CLOSE — MORNING COLLECTIONS BRIEF" in res.text

    def test_download_sales_csv(
        self,
        client: TestClient,
        web_tenant: Tenant,
        web_owner: User,
    ):
        headers = {"x-tenant-id": str(web_tenant.id), "x-user-role": "owner"}
        res = client.get("/exports/sales.csv", headers=headers)
        assert res.status_code == 200
        assert "text/csv" in res.headers["content-type"]
        assert "Invoice Number" in res.text

    def test_download_payments_csv(
        self,
        client: TestClient,
        web_tenant: Tenant,
        web_owner: User,
    ):
        headers = {"x-tenant-id": str(web_tenant.id), "x-user-role": "owner"}
        res = client.get("/exports/payments.csv", headers=headers)
        assert res.status_code == 200
        assert "text/csv" in res.headers["content-type"]
        assert "Payment ID" in res.text

    def test_download_accountant_csv(
        self,
        client: TestClient,
        web_tenant: Tenant,
        web_owner: User,
    ):
        headers = {"x-tenant-id": str(web_tenant.id), "x-user-role": "owner"}
        res = client.get("/exports/accountant.csv", headers=headers)
        assert res.status_code == 200
        assert "text/csv" in res.headers["content-type"]
        assert "PARTNER ACCOUNTANT EXPORT" in res.text
