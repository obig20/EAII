"""
tests/channels/test_telegram.py

Tests for Telegram channel adapter:
  - Invite-code registration
  - Quick-entry parser and sale recording ("INV381 | Abebe Shop | 120000 | due 30d")
  - Photo intake stored strictly as Evidence Level L4 (unverified)
  - /aging and /brief report commands
  - Review queue inline keyboard actions (approve, unverified, reject)
  - Amharic and English localization
  - Strict Rule 2 enforcement on Telegram approval paths
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.channels.telegram.auth import (
    generate_invite_code,
    get_user_by_telegram_id,
    get_user_language,
    redeem_invite_code,
    set_user_language,
)
from app.channels.telegram.bot import (
    get_review_keyboard,
    handle_approve_callback,
    handle_brief,
    handle_photo,
    handle_reject_callback,
    handle_start,
    handle_text,
    handle_unverified_callback,
)
from app.channels.telegram.i18n import t
from app.channels.telegram.quick_entry import parse_quick_entry_text, process_quick_entry
from app.kernel.enums import EvidenceLevel, InvoiceState, MatchState, PaymentState, UserRole
from app.kernel.guards import InsufficientEvidenceError, assert_confirmable
from app.kernel.models import ImportBatch, Invoice, PaymentLine, PaymentMatch, Retailer, Tenant, User


@pytest.fixture(autouse=True)
def bind_bot_session(db_session: Session, monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def _test_session_scope():
        yield db_session

    monkeypatch.setattr("app.channels.telegram.bot.get_session_scope", _test_session_scope)


@pytest.fixture
def tg_tenant(db_session: Session) -> Tenant:
    t = Tenant(id=uuid.uuid4(), name="Telegram Test Wholesaler", created_at=datetime.now(timezone.utc))
    db_session.add(t)
    db_session.flush()
    return t


class TestTelegramQuickEntry:
    def test_parse_quick_entry_standard_format(self):
        text = "INV381 | Abebe Shop | 120000 | due 30d"
        today = date(2026, 10, 26)
        inv_num, retailer, santim, due_date = parse_quick_entry_text(text, base_date=today)

        assert inv_num == "INV381"
        assert retailer == "Abebe Shop"
        assert santim == 120000
        assert due_date == today + timedelta(days=30)

    def test_parse_quick_entry_decimal_etb_and_explicit_date(self):
        text = "INV-402 | Almaz Grocery | 2500.50 ETB | due 2026-11-20"
        inv_num, retailer, santim, due_date = parse_quick_entry_text(text)

        assert inv_num == "INV-402"
        assert retailer == "Almaz Grocery"
        assert santim == 250050
        assert due_date == date(2026, 11, 20)

    def test_parse_quick_entry_syntax_error(self):
        with pytest.raises(ValueError, match="Expected format"):
            parse_quick_entry_text("INV381, Not pipe separated")

    def test_process_quick_entry_creates_invoice_and_retailer(
        self,
        db_session: Session,
        tg_tenant: Tenant,
        owner_user_a: User,
    ):
        text = "INV-QE-1 | New Retailer Addis | 500000 | due 15d"
        res = process_quick_entry(
            db_session,
            tenant_id=tg_tenant.id,
            user_id=owner_user_a.id,
            text=text,
            base_date=date(2026, 10, 26),
        )

        assert res.error is None
        assert res.invoice is not None
        assert res.invoice.invoice_number == "INV-QE-1"
        assert res.invoice.amount_santim == 500000
        assert res.retailer.name == "New Retailer Addis"

    def test_process_quick_entry_rejects_duplicate_invoice(
        self,
        db_session: Session,
        tg_tenant: Tenant,
        owner_user_a: User,
    ):
        text = "INV-DUP-TG | Shop Test | 100000 | due 30d"
        res1 = process_quick_entry(
            db_session,
            tenant_id=tg_tenant.id,
            user_id=owner_user_a.id,
            text=text,
        )
        assert res1.error is None

        # Repeat same invoice number
        res2 = process_quick_entry(
            db_session,
            tenant_id=tg_tenant.id,
            user_id=owner_user_a.id,
            text=text,
        )
        assert res2.error is not None
        assert "already exists" in res2.error.lower()


class TestTelegramLocalization:
    def test_english_localization(self):
        msg = t("welcome", lang="en")
        assert "Welcome to Daily Close" in msg
        assert "Addis Ababa" in msg

        gap_msg = t("quick_entry_gap_warning", lang="en", gaps="INV-002, INV-003")
        assert "INV-002, INV-003" in gap_msg

    def test_amharic_localization(self):
        msg = t("welcome", lang="am")
        assert "ዕለታዊ ሂሳብ ማጠቃለያ" in msg

        gap_msg = t("quick_entry_gap_warning", lang="am", gaps="INV-002")
        assert "ቅደም ተከተል ክፍተት" in gap_msg
        assert "INV-002" in gap_msg

    def test_language_switching(self):
        tg_id = 99887766
        assert get_user_language(tg_id) == "en"

        set_user_language(tg_id, "am")
        assert get_user_language(tg_id) == "am"

        set_user_language(tg_id, "en")
        assert get_user_language(tg_id) == "en"


class TestTelegramInviteRegistration:
    def test_generate_and_redeem_invite_code(
        self,
        db_session: Session,
        tg_tenant: Tenant,
    ):
        code = generate_invite_code(
            tenant_id=tg_tenant.id,
            role=UserRole.CASHIER.value,
            display_name="Kassahun Cashier",
        )
        assert code.startswith("DC-")

        tg_user_id = 123456789
        res = redeem_invite_code(db_session, code=code, telegram_id=tg_user_id)
        assert res is not None
        tenant, user = res
        assert tenant.id == tg_tenant.id
        assert user.role == UserRole.CASHIER.value
        assert user.telegram_id == tg_user_id

        # Re-lookup by telegram ID
        looked_up = get_user_by_telegram_id(db_session, telegram_id=tg_user_id)
        assert looked_up is not None
        assert looked_up[1].id == user.id

    def test_invalid_invite_code_rejected(self, db_session: Session):
        res = redeem_invite_code(db_session, code="DC-INVALID", telegram_id=999)
        assert res is None


class TestTelegramL4EvidenceAndGuards:
    @pytest.mark.asyncio
    async def test_photo_intake_creates_l4_proposed_only(
        self,
        db_session: Session,
        tg_tenant: Tenant,
    ):
        """
        NON-NEGOTIABLE RULE 2:
        Photo receipts sent on Telegram MUST be recorded as Evidence Level L4,
        status PROPOSED. It can NEVER auto-confirm or be set to confirmed.
        """
        # Create user linked to tg_id
        tg_id = 55443322
        user = User(
            id=uuid.uuid4(),
            tenant_id=tg_tenant.id,
            role=UserRole.OWNER.value,
            display_name="TG Owner",
            telegram_id=tg_id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.flush()

        # Mock Telegram Message with Photo
        message = MagicMock()
        message.from_user.id = tg_id
        message.caption = "Receipt 250000 Abebe Shop"
        photo_size = MagicMock()
        photo_size.file_id = "PHOTO_FILE_ID_12345"
        message.photo = [photo_size]
        message.answer = AsyncMock()

        await handle_photo(message)

        # Verify DB entry
        pl = db_session.execute(
            select(PaymentLine).where(
                PaymentLine.tenant_id == tg_tenant.id,
                PaymentLine.source_ref == "TG-PHOTO_FILE",
            )
        ).scalar_one()

        assert pl.evidence_level == EvidenceLevel.L4.value  # Must be L4!
        assert pl.state == PaymentState.PROPOSED.value      # Must be proposed!

        # Invariant check: Assert confirmable on this L4 line MUST raise error!
        with pytest.raises(InsufficientEvidenceError):
            assert_confirmable(EvidenceLevel(pl.evidence_level))

    @pytest.mark.asyncio
    async def test_telegram_approve_blocks_l4_match(
        self,
        db_session: Session,
        tg_tenant: Tenant,
    ):
        """
        Clicking 'Confirm' on an L4 match in Telegram MUST be blocked per Rule 2!
        """
        tg_id = 77665544
        user = User(
            id=uuid.uuid4(),
            tenant_id=tg_tenant.id,
            role=UserRole.OWNER.value,
            display_name="TG Owner",
            telegram_id=tg_id,
            created_at=datetime.now(timezone.utc),
        )
        ret = Retailer(
            id=uuid.uuid4(),
            tenant_id=tg_tenant.id,
            name="Retailer Guard",
            created_at=datetime.now(timezone.utc),
        )
        inv = Invoice(
            id=uuid.uuid4(),
            tenant_id=tg_tenant.id,
            retailer_id=ret.id,
            invoice_number="INV-TG-GUARD",
            amount_santim=200000,
            issued_date=date(2026, 10, 1),
            due_date=date(2026, 10, 31),
            state=InvoiceState.OPEN.value,
            created_at=datetime.now(timezone.utc),
            created_by=user.id,
        )
        batch = ImportBatch(
            id=uuid.uuid4(),
            tenant_id=tg_tenant.id,
            source_type="telegram_photo",
            evidence_level=EvidenceLevel.L4.value,
            imported_by=user.id,
            imported_at=datetime.now(timezone.utc),
        )
        pl = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=tg_tenant.id,
            import_batch_id=batch.id,
            value_date=date(2026, 10, 20),
            amount_santim=200000,
            evidence_level=EvidenceLevel.L4.value,  # L4!
            state=PaymentState.PROPOSED.value,
            created_at=datetime.now(timezone.utc),
        )
        pm = PaymentMatch(
            id=uuid.uuid4(),
            tenant_id=tg_tenant.id,
            payment_line_id=pl.id,
            invoice_id=inv.id,
            matched_santim=200000,
            tier="tier_c",
            state=MatchState.PROPOSED.value,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.flush()
        db_session.add_all([ret, inv, batch, pl, pm])
        db_session.flush()

        callback = MagicMock()
        callback.data = f"approve:{pm.id}"
        callback.from_user.id = tg_id
        callback.answer = AsyncMock()

        await handle_approve_callback(callback)

        # Verify callback was answered with Rule 2 warning
        callback.answer.assert_called_once()
        alert_text = callback.answer.call_args[0][0]
        assert "rule 2" in alert_text.lower() or "cannot be confirmed" in alert_text.lower()

        # Invariant check: PM and PL remain UNCONFIRMED
        db_session.refresh(pm)
        db_session.refresh(pl)
        assert pm.state != MatchState.CONFIRMED.value
        assert pl.state != PaymentState.CONFIRMED.value

    @pytest.mark.asyncio
    async def test_telegram_owner_can_mark_l4_as_accepted_unverified(
        self,
        db_session: Session,
        tg_tenant: Tenant,
    ):
        tg_id = 88990011
        user = User(
            id=uuid.uuid4(),
            tenant_id=tg_tenant.id,
            role=UserRole.OWNER.value,  # OWNER
            display_name="TG Owner",
            telegram_id=tg_id,
            created_at=datetime.now(timezone.utc),
        )
        ret = Retailer(id=uuid.uuid4(), tenant_id=tg_tenant.id, name="Retailer Unver", created_at=datetime.now(timezone.utc))
        inv = Invoice(
            id=uuid.uuid4(),
            tenant_id=tg_tenant.id,
            retailer_id=ret.id,
            invoice_number="INV-UNVER",
            amount_santim=100000,
            issued_date=date(2026, 10, 1),
            due_date=date(2026, 10, 31),
            state=InvoiceState.OPEN.value,
            created_at=datetime.now(timezone.utc),
            created_by=user.id,
        )
        batch = ImportBatch(
            id=uuid.uuid4(),
            tenant_id=tg_tenant.id,
            source_type="telegram_photo",
            evidence_level=EvidenceLevel.L4.value,
            imported_by=user.id,
            imported_at=datetime.now(timezone.utc),
        )
        pl = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=tg_tenant.id,
            import_batch_id=batch.id,
            value_date=date(2026, 10, 20),
            amount_santim=100000,
            evidence_level=EvidenceLevel.L4.value,
            state=PaymentState.PROPOSED.value,
            created_at=datetime.now(timezone.utc),
        )
        pm = PaymentMatch(
            id=uuid.uuid4(),
            tenant_id=tg_tenant.id,
            payment_line_id=pl.id,
            invoice_id=inv.id,
            matched_santim=100000,
            tier="tier_c",
            state=MatchState.PROPOSED.value,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.flush()
        db_session.add_all([ret, inv, batch, pl, pm])
        db_session.flush()

        callback = MagicMock()
        callback.data = f"unverified:{pm.id}"
        callback.from_user.id = tg_id
        callback.answer = AsyncMock()
        callback.message.edit_reply_markup = AsyncMock()
        callback.message.reply = AsyncMock()

        await handle_unverified_callback(callback)

        db_session.refresh(pl)
        assert pl.state == PaymentState.ACCEPTED_UNVERIFIED.value
        callback.message.reply.assert_called_once()
        assert "accepted_unverified" in callback.message.reply.call_args[0][0]

    @pytest.mark.asyncio
    async def test_telegram_cashier_cannot_mark_l4_as_accepted_unverified(
        self,
        db_session: Session,
        tg_tenant: Tenant,
    ):
        tg_id = 99112233
        cashier = User(
            id=uuid.uuid4(),
            tenant_id=tg_tenant.id,
            role=UserRole.CASHIER.value,  # CASHIER
            display_name="TG Cashier",
            telegram_id=tg_id,
            created_at=datetime.now(timezone.utc),
        )
        ret = Retailer(
            id=uuid.uuid4(),
            tenant_id=tg_tenant.id,
            name="Retailer Cashier Test",
            created_at=datetime.now(timezone.utc),
        )
        inv = Invoice(
            id=uuid.uuid4(),
            tenant_id=tg_tenant.id,
            retailer_id=ret.id,
            invoice_number="INV-CASH-TEST",
            amount_santim=100000,
            issued_date=date(2026, 10, 1),
            due_date=date(2026, 10, 31),
            state=InvoiceState.OPEN.value,
            created_at=datetime.now(timezone.utc),
            created_by=cashier.id,
        )
        batch = ImportBatch(
            id=uuid.uuid4(),
            tenant_id=tg_tenant.id,
            source_type="telegram_photo",
            evidence_level=EvidenceLevel.L4.value,
            imported_by=cashier.id,
            imported_at=datetime.now(timezone.utc),
        )
        pl = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=tg_tenant.id,
            import_batch_id=batch.id,
            value_date=date(2026, 10, 20),
            amount_santim=100000,
            evidence_level=EvidenceLevel.L4.value,
            state=PaymentState.PROPOSED.value,
            created_at=datetime.now(timezone.utc),
        )
        pm = PaymentMatch(
            id=uuid.uuid4(),
            tenant_id=tg_tenant.id,
            payment_line_id=pl.id,
            invoice_id=inv.id,
            matched_santim=100000,
            tier="tier_c",
            state=MatchState.PROPOSED.value,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(cashier)
        db_session.flush()
        db_session.add_all([ret, inv, batch, pl, pm])
        db_session.flush()

        callback = MagicMock()
        callback.data = f"unverified:{pm.id}"
        callback.from_user.id = tg_id
        callback.answer = AsyncMock()

        await handle_unverified_callback(callback)

        callback.answer.assert_called_once()
        alert = callback.answer.call_args[0][0]
        assert "only owners" in alert.lower()

        db_session.refresh(pl)
        assert pl.state != PaymentState.ACCEPTED_UNVERIFIED.value
