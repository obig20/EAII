"""
app/channels/telegram/bot.py

Aiogram 3 Telegram Bot adapter for Daily Close.
Features:
  - Invite-code registration
  - Quick-entry credit sales ("INV381 | Abebe Shop | 120000 | due 30d")
  - Photo intake stored strictly as Evidence Level L4 (unverified)
  - /aging and /brief report commands
  - Review queue with Inline Keyboard approval/unverified/rejection buttons
  - English and Amharic localization

Architectural Invariant:
  Business logic in app/kernel/, app/reports/, and app/matching/ NEVER imports
  aiogram or this module.
"""

from __future__ import annotations

import html
import hmac
import io
import re
import uuid
from datetime import date, datetime, timezone
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.channels.telegram.auth import (
    get_user_by_telegram_id,
    get_user_language,
    redeem_invite_code,
    set_user_language,
)
from app.channels.telegram.i18n import t
from app.channels.telegram.quick_entry import process_quick_entry
from app.importers.base import parse_amount_santim
from app.kernel.db import session_scope
from app.kernel.enums import EvidenceLevel, InvoiceState, MatchState, PaymentState, UserRole
from app.kernel.events import append_event
from app.kernel.guards import InsufficientEvidenceError, assert_confirmable
from app.kernel.models import ImportBatch, Invoice, PaymentLine, PaymentMatch, Retailer, User
from app.kernel.money import to_etb
from app.kernel.tenant import set_tenant
from app.reports.aging import generate_aging_report
from app.reports.morning_brief import generate_morning_brief_text

# Session factory hook (allows test suite to bind transactional test session)
get_session_scope = session_scope

router = Router(name="daily_close_telegram")


def get_review_keyboard(match_id: uuid.UUID) -> InlineKeyboardMarkup:
    """Build inline keyboard for reviewing a proposed payment match."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✓ Confirm (L1-L3)", callback_data=f"approve:{match_id}"),
                InlineKeyboardButton(text="⚠️ Accept Unverified (Owner)", callback_data=f"unverified:{match_id}"),
            ],
            [
                InlineKeyboardButton(text="✕ Reject", callback_data=f"reject:{match_id}"),
            ],
        ]
    )


# ---------------------------------------------------------------------------
# Command Handlers
# ---------------------------------------------------------------------------
@router.message(CommandStart())
async def handle_start(message: Message):
    tg_id = message.from_user.id
    lang = get_user_language(tg_id)

    # Check if start command has invite code payload, e.g. /start DC-1234
    args = message.text.split(maxsplit=1)
    if len(args) > 1:
        code = args[1].strip()
        with get_session_scope() as session:
            res = redeem_invite_code(session, code=code, telegram_id=tg_id)
            if res:
                tenant, user = res
                await message.answer(
                    t("registered", lang, role=user.role.upper(), tenant=tenant.name)
                )
                return
            else:
                await message.answer(t("invalid_invite_code", lang))
                return

    with get_session_scope() as session:
        user_info = get_user_by_telegram_id(session, tg_id)

    if user_info:
        tenant, user = user_info
        await message.answer(
            f"{t('welcome', lang)}\n\n"
            f"👤 {user.display_name} ({user.role.upper()}) @ {tenant.name}\n\n"
            f"{t('help_text', lang)}"
        )
    else:
        await message.answer(
            f"{t('welcome', lang)}\n\n{t('ask_invite_code', lang)}"
        )


@router.message(Command("help"))
async def handle_help(message: Message):
    lang = get_user_language(message.from_user.id)
    await message.answer(t("help_text", lang))


@router.message(Command("lang"))
async def handle_lang(message: Message):
    parts = message.text.strip().split()
    if len(parts) > 1 and parts[1].lower() in ("am", "amharic"):
        set_user_language(message.from_user.id, "am")
        await message.answer("ቋንቋ ወደ አማርኛ ተቀይሯል።")
    else:
        set_user_language(message.from_user.id, "en")
        await message.answer("Language set to English.")


@router.message(Command("brief"))
async def handle_brief(message: Message):
    tg_id = message.from_user.id
    lang = get_user_language(tg_id)

    with get_session_scope() as session:
        user_info = get_user_by_telegram_id(session, tg_id)
        if not user_info:
            await message.answer(t("ask_invite_code", lang))
            return
        tenant, _ = user_info
        set_tenant(session, tenant.id)
        brief_text = generate_morning_brief_text(session, tenant_id=tenant.id)

    await message.answer(f"<code>{html.escape(brief_text)}</code>", parse_mode="HTML")


@router.message(Command("aging"))
async def handle_aging(message: Message):
    tg_id = message.from_user.id
    lang = get_user_language(tg_id)

    with get_session_scope() as session:
        user_info = get_user_by_telegram_id(session, tg_id)
        if not user_info:
            await message.answer(t("ask_invite_code", lang))
            return
        tenant, _ = user_info
        set_tenant(session, tenant.id)
        report = generate_aging_report(session, tenant_id=tenant.id)

    lines = [
        t("aging_header", lang),
        f"Total Outstanding: {report.grand_total_outstanding_etb}",
        f"Total Overdue    : {report.grand_total_overdue_etb}",
        "--------------------------------",
        f"• Current (Not Due) : {to_etb(report.total_current_santim)}",
        f"• 1 - 30 days       : {to_etb(report.total_1_30_santim)}",
        f"• 31 - 60 days      : {to_etb(report.total_31_60_santim)}",
        f"• 61 - 90 days      : {to_etb(report.total_61_90_santim)}",
        f"• 90+ days          : {to_etb(report.total_90_plus_santim)}",
        "--------------------------------",
        f"Debtors: {len(report.retailers)} registered retailer accounts",
    ]
    await message.answer("\n".join(lines))


@router.message(Command("review"))
async def handle_review(message: Message):
    tg_id = message.from_user.id
    lang = get_user_language(tg_id)

    with get_session_scope() as session:
        user_info = get_user_by_telegram_id(session, tg_id)
        if not user_info:
            await message.answer(t("ask_invite_code", lang))
            return
        tenant, user = user_info
        if user.role != UserRole.OWNER.value:
            await message.answer("Only owners can review proposed matches.")
            return

        set_tenant(session, tenant.id)

        # Query proposed matches
        matches = session.execute(
            select(PaymentMatch)
            .join(PaymentLine, PaymentMatch.payment_line_id == PaymentLine.id)
            .where(
                PaymentMatch.tenant_id == tenant.id,
                PaymentMatch.state == MatchState.PROPOSED.value,
            )
            .limit(5)
        ).scalars().all()

        if not matches:
            await message.answer("✓ Review queue is clear! Zero pending matches for approval.")
            return

        for pm in matches:
            pl = pm.payment_line
            inv = pm.invoice
            kb = get_review_keyboard(pm.id)
            ret_name = html.escape(inv.retailer.name if inv.retailer else "Unknown")
            inv_num = html.escape(inv.invoice_number)
            tier_str = html.escape(pm.tier.upper())
            ev_str = html.escape(str(pl.evidence_level))
            ref_str = html.escape(pl.raw_reference or pl.source_ref or "None")
            text_block = (
                f"🔍 <b>Proposed Match ({tier_str})</b>\n"
                f"Amount: <b>{to_etb(pm.matched_santim)}</b>\n"
                f"Evidence: <code>{ev_str}</code> ({'Verified' if pl.evidence_level in ('L1','L2','L3') else 'Unverified L4'})\n"
                f"Invoice: <b>{inv_num}</b> (Retailer: {ret_name})\n"
                f"Payment Reference: {ref_str}\n"
                f"Date: {pl.value_date.isoformat()}"
            )
            await message.answer(text_block, parse_mode="HTML", reply_markup=kb)


# ---------------------------------------------------------------------------
# Callback Query Handlers (Inline Buttons)
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("approve:"))
async def handle_approve_callback(callback: CallbackQuery):
    match_id_str = callback.data.split(":")[1]
    tg_id = callback.from_user.id
    lang = get_user_language(tg_id)

    with get_session_scope() as session:
        user_info = get_user_by_telegram_id(session, tg_id)
        if not user_info:
            await callback.answer(t("ask_invite_code", lang), show_alert=True)
            return
        tenant, user = user_info
        if user.role != UserRole.OWNER.value:
            await callback.answer("Only owners can approve payment matches.", show_alert=True)
            return

        set_tenant(session, tenant.id)

        try:
            mid = uuid.UUID(match_id_str)
        except ValueError:
            await callback.answer("Invalid match ID", show_alert=True)
            return

        # 1. Lock PaymentMatch in transaction (SEC-02 consistent lock order: match -> line -> invoice)
        pm = session.execute(
            select(PaymentMatch)
            .where(PaymentMatch.id == mid, PaymentMatch.tenant_id == tenant.id)
            .with_for_update()
        ).scalar_one_or_none()

        if not pm:
            await callback.answer("Match not found", show_alert=True)
            return

        # Idempotency
        if pm.state == MatchState.CONFIRMED.value:
            await callback.answer("Already confirmed!", show_alert=True)
            return

        # 2. Lock PaymentLine in consistent order (SEC-02)
        pl = session.execute(
            select(PaymentLine)
            .where(PaymentLine.id == pm.payment_line_id, PaymentLine.tenant_id == tenant.id)
            .with_for_update()
        ).scalar_one()

        # NON-NEGOTIABLE RULE 2: L4 Evidence cannot confirm!
        try:
            assert_confirmable(EvidenceLevel(pl.evidence_level))
        except InsufficientEvidenceError:
            await callback.answer(t("l4_cannot_confirm", lang), show_alert=True)
            return

        # Check payment allocations
        from sqlalchemy import func
        allocated_santim = session.execute(
            select(func.coalesce(func.sum(PaymentMatch.matched_santim), 0))
            .where(
                PaymentMatch.payment_line_id == pl.id,
                PaymentMatch.state == MatchState.CONFIRMED.value,
                PaymentMatch.id != pm.id,
            )
        ).scalar_one()

        if allocated_santim + pm.matched_santim > pl.amount_santim:
            await callback.answer("Payment line already confirmed or fully allocated.", show_alert=True)
            return

        # 3. Lock Invoice in consistent order (SEC-02)
        inv = session.execute(
            select(Invoice)
            .where(Invoice.id == pm.invoice_id, Invoice.tenant_id == tenant.id)
            .with_for_update()
        ).scalar_one()

        now = datetime.now(timezone.utc)
        pm.state = MatchState.CONFIRMED.value
        pm.confirmed_by = user.id
        pm.confirmed_at = now
        pl.state = PaymentState.CONFIRMED.value

        # Settle invoice if completed
        paid = session.execute(
            select(func.coalesce(func.sum(PaymentMatch.matched_santim), 0))
            .where(PaymentMatch.invoice_id == inv.id, PaymentMatch.state == MatchState.CONFIRMED.value)
        ).scalar_one()

        if int(paid) >= inv.amount_santim:
            inv.state = InvoiceState.SETTLED.value
        else:
            inv.state = InvoiceState.PARTIALLY_PAID.value

        append_event(
            session,
            tenant_id=tenant.id,
            event_type="match.telegram_confirmed",
            aggregate_type="payment_match",
            aggregate_id=pm.id,
            actor_id=user.id,
            payload={"invoice_id": str(inv.id), "amount_santim": pm.matched_santim},
        )

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.reply(t("match_confirmed", lang))
    await callback.answer("Confirmed!")


@router.callback_query(F.data.startswith("unverified:"))
async def handle_unverified_callback(callback: CallbackQuery):
    match_id_str = callback.data.split(":")[1]
    tg_id = callback.from_user.id
    lang = get_user_language(tg_id)

    with get_session_scope() as session:
        user_info = get_user_by_telegram_id(session, tg_id)
        if not user_info:
            await callback.answer(t("ask_invite_code", lang), show_alert=True)
            return
        tenant, user = user_info
        if user.role != UserRole.OWNER.value:
            await callback.answer(t("only_owner_can_accept_unverified", lang), show_alert=True)
            return

        set_tenant(session, tenant.id)
        try:
            mid = uuid.UUID(match_id_str)
        except ValueError:
            await callback.answer("Invalid match ID", show_alert=True)
            return

        pm = session.get(PaymentMatch, mid)
        if not pm or pm.tenant_id != tenant.id:
            await callback.answer("Match not found", show_alert=True)
            return

        pl = pm.payment_line
        pl.state = PaymentState.ACCEPTED_UNVERIFIED.value

        append_event(
            session,
            tenant_id=tenant.id,
            event_type="payment.accepted_unverified_by_owner",
            aggregate_type="payment_line",
            aggregate_id=pl.id,
            actor_id=user.id,
            payload={"payment_id": str(pl.id), "amount_santim": pl.amount_santim},
        )

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.reply(t("match_accepted_unverified", lang))
    await callback.answer("Marked accepted_unverified")


@router.callback_query(F.data.startswith("reject:"))
async def handle_reject_callback(callback: CallbackQuery):
    match_id_str = callback.data.split(":")[1]
    tg_id = callback.from_user.id
    lang = get_user_language(tg_id)

    with get_session_scope() as session:
        user_info = get_user_by_telegram_id(session, tg_id)
        if not user_info:
            await callback.answer(t("ask_invite_code", lang), show_alert=True)
            return
        tenant, user = user_info
        if user.role != UserRole.OWNER.value:
            await callback.answer("Only owners can reject payment matches.", show_alert=True)
            return

        set_tenant(session, tenant.id)

        try:
            mid = uuid.UUID(match_id_str)
        except ValueError:
            await callback.answer("Invalid match ID", show_alert=True)
            return

        pm = session.get(PaymentMatch, mid)
        if pm and pm.tenant_id == tenant.id:
            pm.state = MatchState.REJECTED.value
            pm.payment_line.state = PaymentState.REJECTED.value

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.reply(t("match_rejected", lang))
    await callback.answer("Rejected")


# ---------------------------------------------------------------------------
# Photo Message Handler (L4 Evidence Capture)
# ---------------------------------------------------------------------------
@router.message(F.photo)
async def handle_photo(message: Message):
    """
    Handle receipt screenshot / forwarded SMS photo.
    Strictly records payment line as Evidence Level L4 (unverified).
    """
    tg_id = message.from_user.id
    lang = get_user_language(tg_id)

    with get_session_scope() as session:
        user_info = get_user_by_telegram_id(session, tg_id)
        if not user_info:
            await message.answer(t("ask_invite_code", lang))
            return
        tenant, user = user_info
        set_tenant(session, tenant.id)

        now = datetime.now(timezone.utc)
        photo = message.photo[-1]  # Highest resolution
        file_id = photo.file_id

        # Try parsing amount from caption if provided (e.g. "120000 Abebe" or "1500 ETB")
        caption = message.caption or ""
        amount_santim = 100000  # Default 1,000.00 ETB if unspecified in caption
        amt_match = re.search(r"(\d+(?:\.\d{1,2})?)", caption)
        if amt_match:
            try:
                raw_amt = amt_match.group(1)
                amount_santim = parse_amount_santim(raw_amt) if "." in raw_amt else int(raw_amt)
            except Exception:
                pass

        batch = ImportBatch(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            source_type="telegram_photo_receipt",
            evidence_level=EvidenceLevel.L4.value,  # L4!
            filename=f"tg_photo_{file_id[:12]}.jpg",
            imported_by=user.id,
            imported_at=now,
        )
        pl = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            import_batch_id=batch.id,
            value_date=now.date(),
            amount_santim=amount_santim,
            source_ref=f"TG-{file_id[:10]}",
            counterparty_name=caption[:50] if caption else None,
            raw_reference=caption or "Telegram photo receipt / SMS screenshot",
            evidence_level=EvidenceLevel.L4.value,  # L4!
            state=PaymentState.PROPOSED.value,      # PROPOSED only per Rule 2!
            created_at=now,
        )
        session.add_all([batch, pl])
        session.flush()

        append_event(
            session,
            tenant_id=tenant.id,
            event_type="payment.l4_photo_received",
            aggregate_type="payment_line",
            aggregate_id=pl.id,
            actor_id=user.id,
            evidence_level=EvidenceLevel.L4,
            payload={
                "file_id": file_id,
                "amount_santim": amount_santim,
                "caption": caption,
            },
        )

    await message.answer(
        t("photo_received_l4", lang, amount_etb=to_etb(amount_santim))
    )


# ---------------------------------------------------------------------------
# Text Message Handler (Invite Codes & Quick Entry)
# ---------------------------------------------------------------------------
@router.message(F.text)
async def handle_text(message: Message):
    tg_id = message.from_user.id
    raw_text = message.text.strip()
    lang = get_user_language(tg_id)

    # 1. Check if user is submitting an invite code (starts with DC- or alphanumeric)
    if raw_text.upper().startswith("DC-"):
        with get_session_scope() as session:
            res = redeem_invite_code(session, code=raw_text, telegram_id=tg_id)
            if res:
                tenant, user = res
                await message.answer(
                    t("registered", lang, role=user.role.upper(), tenant=tenant.name)
                )
                return
            else:
                await message.answer(t("invalid_invite_code", lang))
                return

    # 2. Check if text is a quick entry ("INV... | ... | ...")
    if "|" in raw_text:
        with get_session_scope() as session:
            user_info = get_user_by_telegram_id(session, tg_id)
            if not user_info:
                await message.answer(t("ask_invite_code", lang))
                return
            tenant, user = user_info
            set_tenant(session, tenant.id)

            res = process_quick_entry(
                session,
                tenant_id=tenant.id,
                user_id=user.id,
                text=raw_text,
            )

            if res.error:
                if "already exists" in res.error.lower():
                    await message.answer(
                        t("quick_entry_duplicate_error", lang, invoice_number=raw_text.split("|")[0].strip())
                    )
                else:
                    await message.answer(f"✕ {res.error}")
                return

            inv = res.invoice
            response_text = t(
                "quick_entry_success",
                lang,
                invoice_number=inv.invoice_number,
                retailer_name=res.retailer.name,
                amount_etb=to_etb(inv.amount_santim),
                due_date=inv.due_date.isoformat(),
            )
            if res.gaps:
                response_text += "\n" + t("quick_entry_gap_warning", lang, gaps=", ".join(res.gaps[:3]))

        await message.answer(response_text)
        return

    # 3. Default fallback
    await message.answer(t("help_text", lang))


def create_telegram_bot(token: str) -> tuple[Bot, Dispatcher]:
    """Factory creating bot and dispatcher configured with router."""
    bot = Bot(token=token)
    dp = Dispatcher()
    dp.include_router(router)
    return bot, dp


def verify_telegram_webhook_secret(secret_header: str | None, expected_secret: str) -> bool:
    """Verify Telegram webhook X-Telegram-Bot-Api-Secret-Token header."""
    if not expected_secret:
        return True
    if not secret_header:
        return False
    return hmac.compare_digest(secret_header.strip(), expected_secret.strip())

