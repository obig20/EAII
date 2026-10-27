"""
tests/channels/test_demo_mode.py

Comprehensive tests for Stage 2: Demo Mode
- Creation & Population of realistic Addis wholesale dataset:
  * 12 Ethiopian retailers (Mercato, Piassa, Bole, etc.)
  * 30 invoices spread across all 5 aging buckets (15k - 450k ETB)
  * 9 payment lines with deterministic matching tiers:
      - 3 Tier A auto-confirmed
      - 2 Tier B in review queue
      - 1 Tier C in review queue
      - 1 Partial payment (differing warning)
      - 1 Overpayment with unallocated credit remainder
      - 1 L4 cash receipt (cannot auto-confirm)
- Hard safety guard: Non-demo tenants cannot be reset (raises PermissionError)
- RLS isolation of demo tenants
- Web UI banner & POST /demo/reset endpoint
- Telegram /demo & /reset_demo commands
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.channels.telegram.bot import handle_demo, handle_reset_demo
from app.demo.generator import create_demo_tenant, seed_demo_data
from app.demo.reset import reset_demo_tenant
from app.kernel.db import get_session
from app.kernel.enums import EvidenceLevel, InvoiceState, MatchState, MatchTier, PaymentState, UserRole
from app.kernel.models import ImportBatch, Invoice, PaymentLine, PaymentMatch, Retailer, Tenant, User
from app.kernel.tenant import set_tenant
from app.main import app
from app.reports.aging import generate_aging_report
from tests.conftest import authenticate_client


@pytest.fixture(autouse=True)
def override_telegram_session(monkeypatch, db_session):
    @contextmanager
    def _test_session_scope():
        yield db_session
    monkeypatch.setattr("app.channels.telegram.bot.get_session_scope", _test_session_scope)


@pytest.fixture
def demo_tenant_and_owner(db_session: Session) -> tuple[Tenant, User]:
    now = datetime.now(timezone.utc)
    t = Tenant(
        id=uuid.uuid4(),
        name="Addis Wholesale Demo / አዲስ ጅምላ ማሳያ",
        is_demo=True,
        created_at=now,
    )
    u = User(
        id=uuid.uuid4(),
        tenant_id=t.id,
        role=UserRole.OWNER.value,
        phone="+251911999901",
        email="demo.owner@dailyclose.et",
        display_name="Ato Demo Owner",
        password_hash="dummy",
        is_active=True,
        created_at=now,
    )
    db_session.add_all([t, u])
    db_session.flush()
    set_tenant(db_session, t.id)
    seed_demo_data(db_session, t, u)
    return t, u


@pytest.fixture
def non_demo_tenant_and_owner(db_session: Session) -> tuple[Tenant, User]:
    now = datetime.now(timezone.utc)
    t = Tenant(
        id=uuid.uuid4(),
        name="Real Production Wholesaler PLC",
        is_demo=False,
        created_at=now,
    )
    u = User(
        id=uuid.uuid4(),
        tenant_id=t.id,
        role=UserRole.OWNER.value,
        phone="+251911888802",
        email="real.owner@production.et",
        display_name="Woizero Real Owner",
        password_hash="dummy",
        is_active=True,
        created_at=now,
    )
    db_session.add_all([t, u])
    db_session.flush()
    set_tenant(db_session, t.id)
    return t, u


class TestDemoCreationAndPopulation:
    def test_demo_tenant_generator_retailers_and_invoices(
        self, db_session: Session, demo_tenant_and_owner: tuple[Tenant, User]
    ):
        tenant, owner = demo_tenant_and_owner
        set_tenant(db_session, tenant.id)

        # 1. Assert exactly 12 retailers created with realistic Addis locations
        retailers = db_session.execute(
            select(Retailer).where(Retailer.tenant_id == tenant.id)
        ).scalars().all()
        assert len(retailers) == 12

        # Check phone prefixes (0911, 0912, 0922, 0930)
        valid_prefixes = ("+251911", "+251912", "+251922", "+251930")
        assert all(any(r.phone.startswith(prefix) for prefix in valid_prefixes) for r in retailers)

        # 2. Assert 30 invoices created with wholesale amounts (15k - 450k ETB)
        invoices = db_session.execute(
            select(Invoice).where(Invoice.tenant_id == tenant.id)
        ).scalars().all()
        assert len(invoices) == 30

        for inv in invoices:
            assert inv.amount_santim >= 1500000  # min 15,000 ETB
            assert inv.amount_santim <= 45000000 # max 450,000 ETB

        # 3. Assert all 5 aging buckets are populated
        today = date.today()
        aging_rep = generate_aging_report(db_session, tenant_id=tenant.id, as_of_date=today)
        assert aging_rep.total_current_santim > 0
        assert aging_rep.total_1_30_santim > 0
        assert aging_rep.total_31_60_santim > 0
        assert aging_rep.total_61_90_santim > 0
        assert aging_rep.total_90_plus_santim > 0

    def test_demo_statement_payment_lines_and_tiers(
        self, db_session: Session, demo_tenant_and_owner: tuple[Tenant, User]
    ):
        tenant, owner = demo_tenant_and_owner
        set_tenant(db_session, tenant.id)

        lines = db_session.execute(
            select(PaymentLine).where(PaymentLine.tenant_id == tenant.id)
        ).scalars().all()
        assert len(lines) == 9

        # 3 Tier A lines are auto-confirmed
        confirmed_lines = [l for l in lines if l.state == PaymentState.CONFIRMED.value]
        assert len(confirmed_lines) == 3

        confirmed_matches = db_session.execute(
            select(PaymentMatch).where(
                PaymentMatch.tenant_id == tenant.id,
                PaymentMatch.state == MatchState.CONFIRMED.value,
                PaymentMatch.tier == MatchTier.TIER_A.value,
            )
        ).scalars().all()
        assert len(confirmed_matches) == 3

        # Remaining 6 lines are proposed (in review queue)
        proposed_lines = [l for l in lines if l.state == PaymentState.PROPOSED.value]
        assert len(proposed_lines) == 6

        # Check L4 cash note
        l4_line = [l for l in lines if l.evidence_level == "L4"][0]
        assert "Woizero Almaz brought cash" in l4_line.raw_reference
        assert l4_line.state == PaymentState.PROPOSED.value  # Cannot auto-confirm!


class TestDemoSafetyAndReset:
    def test_reset_demo_tenant_success(
        self, db_session: Session, demo_tenant_and_owner: tuple[Tenant, User]
    ):
        tenant, owner = demo_tenant_and_owner
        set_tenant(db_session, tenant.id)

        # Mutate data: add a dummy invoice and confirm a proposed match
        inv_extra = Invoice(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            invoice_number="INV-MUTATED-999",
            retailer_id=db_session.execute(select(Retailer.id).where(Retailer.tenant_id == tenant.id)).scalar(),
            amount_santim=9900000,
            issued_date=date.today(),
            due_date=date.today() + timedelta(days=30),
            state=InvoiceState.OPEN.value,
            created_at=datetime.now(timezone.utc),
            created_by=owner.id,
        )
        db_session.add(inv_extra)
        db_session.flush()

        invoices_before = db_session.execute(
            select(func.count(Invoice.id)).where(Invoice.tenant_id == tenant.id)
        ).scalar_one()
        assert invoices_before == 31

        # Perform safe reset
        reset_demo_tenant(db_session, tenant.id)

        # Invoices restored to baseline 30
        invoices_after = db_session.execute(
            select(func.count(Invoice.id)).where(Invoice.tenant_id == tenant.id)
        ).scalar_one()
        assert invoices_after == 30

        # Mutated invoice was deleted
        mutated_check = db_session.execute(
            select(Invoice).where(Invoice.invoice_number == "INV-MUTATED-999")
        ).scalar_one_or_none()
        assert mutated_check is None

    def test_reset_non_demo_tenant_strictly_forbidden(
        self, db_session: Session, non_demo_tenant_and_owner: tuple[Tenant, User]
    ):
        real_tenant, real_owner = non_demo_tenant_and_owner
        set_tenant(db_session, real_tenant.id)

        # Seed 1 real retailer & invoice
        r = Retailer(
            id=uuid.uuid4(),
            tenant_id=real_tenant.id,
            name="Real Mercato Retailer",
            phone="+251911999888",
            bank_account="1000999888",
            created_at=datetime.now(timezone.utc),
        )
        inv = Invoice(
            id=uuid.uuid4(),
            tenant_id=real_tenant.id,
            invoice_number="INV-REAL-001",
            retailer_id=r.id,
            amount_santim=5000000,
            issued_date=date.today(),
            due_date=date.today() + timedelta(days=30),
            state=InvoiceState.OPEN.value,
            created_at=datetime.now(timezone.utc),
            created_by=real_owner.id,
        )
        db_session.add_all([r, inv])
        db_session.flush()

        # Attempting reset on a non-demo tenant MUST raise PermissionError
        with pytest.raises(PermissionError) as exc_info:
            reset_demo_tenant(db_session, real_tenant.id)

        assert "Safety Violation" in str(exc_info.value)
        assert "is NOT a demo tenant" in str(exc_info.value)

        # Ensure real data was untouched
        real_inv = db_session.get(Invoice, inv.id)
        assert real_inv is not None
        assert real_inv.invoice_number == "INV-REAL-001"


class TestDemoWebEndpoints:
    def test_web_banner_rendered_only_on_demo_tenant(
        self, client: TestClient, db_session: Session, demo_tenant_and_owner: tuple[Tenant, User], non_demo_tenant_and_owner: tuple[Tenant, User]
    ):
        demo_t, demo_u = demo_tenant_and_owner
        real_t, real_u = non_demo_tenant_and_owner

        # 1. Demo tenant renders banner
        authenticate_client(client, db_session, demo_u)
        res_demo = client.get("/")
        assert res_demo.status_code == 200
        assert "Demo Mode" in res_demo.text
        assert "Addis Ababa Wholesale Demonstration" in res_demo.text
        assert "Reset Demo Data" in res_demo.text

        # 2. Live production tenant does NOT render demo banner
        authenticate_client(client, db_session, real_u)
        res_real = client.get("/")
        assert res_real.status_code == 200
        assert "Demo Mode" not in res_real.text
        assert "Reset Demo Data" not in res_real.text

    def test_post_demo_reset_endpoint(
        self, client: TestClient, db_session: Session, demo_tenant_and_owner: tuple[Tenant, User], non_demo_tenant_and_owner: tuple[Tenant, User]
    ):
        demo_t, demo_u = demo_tenant_and_owner
        real_t, real_u = non_demo_tenant_and_owner

        # 1. Owner resetting demo tenant succeeds
        authenticate_client(client, db_session, demo_u)
        res_ok = client.post("/demo/reset", follow_redirects=False)
        assert res_ok.status_code == 303
        assert "msg=Demo+data+successfully+reset+to+baseline" in res_ok.headers["location"]

        # 2. Owner resetting non-demo tenant returns 400 Bad Request
        authenticate_client(client, db_session, real_u)
        res_bad = client.post("/demo/reset")
        assert res_bad.status_code == 400
        assert "Cannot reset a non-demo tenant" in res_bad.json()["detail"]


@pytest.mark.asyncio
class TestDemoTelegramCommands:
    async def test_telegram_demo_command(self, db_session: Session):
        mock_msg = MagicMock()
        mock_msg.from_user = MagicMock()
        mock_msg.from_user.id = 998877665
        mock_msg.from_user.full_name = "Ato Prospect"
        mock_msg.answer = AsyncMock()

        await handle_demo(mock_msg)
        mock_msg.answer.assert_called_once()
        args, kwargs = mock_msg.answer.call_args
        assert "Welcome to Daily Close Demo Mode" in args[0]
        assert "12 Retailers" in args[0]
        assert "30 Credit Invoices" in args[0]

    async def test_telegram_reset_demo_command(self, db_session: Session):
        mock_msg = MagicMock()
        mock_msg.from_user = MagicMock()
        mock_msg.from_user.id = 998877665
        mock_msg.from_user.full_name = "Ato Prospect"
        mock_msg.answer = AsyncMock()

        # 1. Initialize demo tenant first
        await handle_demo(mock_msg)

        # 2. Reset demo tenant
        mock_msg.answer.reset_mock()
        await handle_reset_demo(mock_msg)
        mock_msg.answer.assert_called_once()
        args, kwargs = mock_msg.answer.call_args
        assert "Demo data successfully reset" in args[0]


class TestCliDemoCommands:
    def test_cli_create_and_reset_demo(self, db_session: Session):
        import argparse
        from app.cli import cli_create_demo, cli_reset_demo

        run_id = uuid.uuid4().hex[:8]
        t_name = f"CLI Addis Demo Wholesaler {run_id}"
        u_email = f"cli.owner.{run_id}@dailyclose.et"

        args_create = argparse.Namespace(
            tenant=t_name,
            identifier=u_email,
            name="CLI Demo Owner",
            password="CliDemoPass1234!",
        )
        assert cli_create_demo(args_create) == 0

        # Verify tenant was created
        tenant = db_session.execute(
            select(Tenant).where(Tenant.name == t_name)
        ).scalar_one()
        assert tenant.is_demo is True

        # Run reset
        args_reset = argparse.Namespace(tenant_id=str(tenant.id))
        assert cli_reset_demo(args_reset) == 0
