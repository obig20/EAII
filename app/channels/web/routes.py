"""
app/channels/web/routes.py

Web routes for Daily Close dashboard, 3 daily close steps, exports, and match review.
"""

from __future__ import annotations

import io
import uuid
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.channels.web.rate_limit import confirmation_rate_limiter
from app.importers.base import parse_amount_santim, parse_statement_date
from app.importers.csv_importer import CsvStatementImporter
from app.importers.mapping import (
    CBE_CSV_CONFIG,
    DASHEN_CSV_CONFIG,
    TELEBIRR_XLSX_CONFIG,
    ColumnMappingConfig,
)
from app.importers.xlsx_importer import XlsxStatementImporter
from app.kernel.db import get_session
from app.kernel.enums import EvidenceLevel, InvoiceState, MatchState, PaymentState, UserRole
from app.kernel.guards import InsufficientEvidenceError, assert_confirmable
from app.kernel.models import ImportBatch, Invoice, PaymentLine, PaymentMatch, Retailer, Tenant, User
from app.kernel.money import to_etb
from app.kernel.retailer_confirmation import (
    DisputeReasonCode,
    TokenAlreadyUsedError,
    TokenError,
    TokenExpiredError,
    TokenNotFoundError,
    confirm_retailer_balance,
    create_retailer_confirmation_token,
    dispute_retailer_balance,
    validate_confirmation_token,
)
from app.kernel.sales import DuplicateInvoiceError, InvalidSaleError, parse_and_record_sales_csv, record_credit_sale
from app.kernel.tenant import set_tenant
from app.matching.matcher import PaymentMatcher
from app.reports.aging import generate_aging_report
from app.reports.exports import export_accountant_csv, export_payments_csv, export_sales_csv
from app.reports.morning_brief import generate_morning_brief_text


TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# File upload security limits
MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB limit
ALLOWED_SALES_EXTENSIONS = {".csv"}
ALLOWED_STATEMENT_EXTENSIONS = {".csv", ".xlsx"}


def validate_uploaded_file(file: UploadFile, allowed_extensions: set[str]) -> str:
    """Validate filename extension and sanitize path."""
    raw_name = file.filename or ""
    safe_name = Path(raw_name).name  # Strips directory traversal (/ or \)
    ext = Path(safe_name).suffix.lower()
    if not ext or ext not in allowed_extensions:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file extension '{ext}'. Allowed extensions: {', '.join(sorted(allowed_extensions))}",
        )
    return safe_name

router = APIRouter()


def get_current_tenant_and_user(
    session: Session = Depends(get_session),
    x_tenant_id: str | None = Header(None),
    x_user_role: str | None = Header(None),
    role: str | None = Query(None),
) -> tuple[Tenant, User]:
    """Resolve current tenant and user context. Creates default if DB empty."""
    # 1. Resolve Tenant
    tenant: Tenant | None = None
    if x_tenant_id:
        try:
            tid = uuid.UUID(x_tenant_id)
            tenant = session.get(Tenant, tid)
        except ValueError:
            pass

    if not tenant:
        tenant = session.execute(select(Tenant).order_by(Tenant.created_at)).scalars().first()

    now = datetime.now(timezone.utc)
    if not tenant:
        tenant = Tenant(id=uuid.uuid4(), name="Birana Addis Wholesalers", created_at=now)
        session.add(tenant)
        session.flush()

    # Set transaction tenant context for RLS
    set_tenant(session, tenant.id)

    # 2. Resolve User & Role
    user_role_str = (role or x_user_role or "owner").lower()
    if user_role_str not in ("owner", "cashier"):
        user_role_str = "owner"

    user = session.execute(
        select(User).where(User.tenant_id == tenant.id, User.role == user_role_str)
    ).scalars().first()

    if not user:
        user = User(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            role=user_role_str,
            display_name=f"Addis {user_role_str.capitalize()}",
            created_at=now,
        )
        session.add(user)
        session.flush()

    return tenant, user


# ---------------------------------------------------------------------------
# Dashboard View
# ---------------------------------------------------------------------------
@router.get("/", response_class=HTMLResponse)
@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    session: Session = Depends(get_session),
    context: tuple[Tenant, User] = Depends(get_current_tenant_and_user),
    msg: str | None = Query(None),
):
    tenant, user = context
    set_tenant(session, tenant.id)
    today = datetime.now(timezone.utc).date()

    # Fetch retailers
    retailers = session.execute(
        select(Retailer).where(Retailer.tenant_id == tenant.id).order_by(Retailer.name)
    ).scalars().all()

    # Generate aging report
    aging_rep = generate_aging_report(session, tenant_id=tenant.id, as_of_date=today)

    # Fetch proposed / review items
    stmt = (
        select(PaymentLine, PaymentMatch)
        .outerjoin(PaymentMatch, PaymentLine.id == PaymentMatch.payment_line_id)
        .where(
            PaymentLine.tenant_id == tenant.id,
            PaymentLine.state.in_([PaymentState.PROPOSED.value, PaymentState.UNMATCHED.value]),
        )
        .order_by(PaymentLine.value_date.desc())
    )
    rows = session.execute(stmt).all()

    proposed_items = []
    seen_payments = set()
    for pl, pm in rows:
        if pl.id in seen_payments:
            continue
        seen_payments.add(pl.id)

        target_invs = "None"
        reason = "Unmatched statement entry"
        is_ambiguous = False
        match_id = str(pm.id) if pm else None

        if pm and pm.invoice:
            target_invs = pm.invoice.invoice_number
            reason = f"Proposed {pm.tier} match"
        elif pl.state == "proposed":
            reason = "Awaiting verification"

        proposed_items.append({
            "payment_id": str(pl.id),
            "match_id": match_id,
            "amount_etb": to_etb(pl.amount_santim),
            "value_date": pl.value_date.strftime("%d %b %Y"),
            "evidence_level": pl.evidence_level,
            "counterparty_name": pl.counterparty_name,
            "counterparty_account": pl.counterparty_account,
            "raw_reference": pl.raw_reference,
            "target_invoice_numbers": target_invs,
            "reason": reason,
            "is_ambiguous": is_ambiguous,
        })

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "tenant_name": tenant.name,
            "user_role": user.role,
            "today_date": today,
            "default_due_date": today + timedelta(days=30),
            "retailers": retailers,
            "aging_report": aging_rep,
            "report": aging_rep,
            "proposed_items": proposed_items,
            "message": msg,
        },
    )


# ---------------------------------------------------------------------------
# Step 1: Credit-Sale Capture Routes
# ---------------------------------------------------------------------------
@router.post("/sales")
def create_sale(
    invoice_number: str = Form(...),
    retailer_id: str = Form(...),
    amount_etb: str = Form(...),
    due_date: str = Form(...),
    session: Session = Depends(get_session),
    context: tuple[Tenant, User] = Depends(get_current_tenant_and_user),
):
    tenant, user = context
    set_tenant(session, tenant.id)

    try:
        ret_uuid = uuid.UUID(retailer_id)
        due_d = parse_statement_date(due_date)
        santim = parse_amount_santim(amount_etb)
        today = datetime.now(timezone.utc).date()

        inv, gaps = record_credit_sale(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            invoice_number=invoice_number,
            retailer_id=ret_uuid,
            amount_santim=santim,
            issued_date=today,
            due_date=due_d,
        )
        session.commit()

        msg = f"Credit sale {inv.invoice_number} recorded successfully."
        if gaps:
            msg += f" Note: Sequence gap detected (missing: {', '.join(gaps[:3])})."
        return RedirectResponse(f"/?msg={msg}", status_code=303)

    except DuplicateInvoiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except (InvalidSaleError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid sale parameters: {exc}")


@router.post("/sales/bulk")
async def bulk_sales(
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    context: tuple[Tenant, User] = Depends(get_current_tenant_and_user),
):
    tenant, user = context
    set_tenant(session, tenant.id)

    validate_uploaded_file(file, ALLOWED_SALES_EXTENSIONS)
    content = await file.read(MAX_UPLOAD_SIZE + 1)
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail="File too large. Maximum allowed size is 10MB.")
    if not content or len(content.strip()) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    created, gaps, errors = parse_and_record_sales_csv(
        session,
        tenant_id=tenant.id,
        user_id=user.id,
        csv_content=content,
    )
    session.commit()

    msg = f"Bulk upload: {len(created)} invoices created."
    if gaps:
        msg += f" Sequence gaps detected: {', '.join(gaps[:3])}."
    if errors:
        msg += f" (Encountered {len(errors)} row errors)."

    return RedirectResponse(f"/?msg={msg}", status_code=303)


# ---------------------------------------------------------------------------
# Step 2: Statement Import & Auto-Matching Route
# ---------------------------------------------------------------------------
@router.post("/statements/upload")
async def upload_statement(
    source_name: str = Form(...),
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    context: tuple[Tenant, User] = Depends(get_current_tenant_and_user),
):
    tenant, user = context
    set_tenant(session, tenant.id)

    safe_filename = validate_uploaded_file(file, ALLOWED_STATEMENT_EXTENSIONS)
    content = await file.read(MAX_UPLOAD_SIZE + 1)
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail="File too large. Maximum allowed size is 10MB.")
    if not content or len(content.strip()) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    # 1. Resolve Config
    cfg_map = {
        "cbe_csv": CBE_CSV_CONFIG,
        "telebirr_xlsx": TELEBIRR_XLSX_CONFIG,
        "dashen_csv": DASHEN_CSV_CONFIG,
    }
    cfg = cfg_map.get(source_name, CBE_CSV_CONFIG)

    # 2. Ingest lines
    if safe_filename.endswith(".xlsx"):
        importer = XlsxStatementImporter(cfg)
    else:
        importer = CsvStatementImporter(cfg)

    try:
        batch, payment_lines = importer.import_statement(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            content_or_path=content,
            filename=safe_filename,
        )
    except (ValueError, zipfile.BadZipFile) as exc:
        raise HTTPException(status_code=400, detail=f"Failed to parse statement: {exc}")

    # 3. Fetch open invoices & retailers for matching
    open_invoices = session.execute(
        select(Invoice).where(
            Invoice.tenant_id == tenant.id,
            Invoice.state.in_(["open", "partially_paid"]),
        )
    ).scalars().all()

    retailers = session.execute(
        select(Retailer).where(Retailer.tenant_id == tenant.id)
    ).scalars().all()

    matcher = PaymentMatcher()
    auto_count = 0
    proposed_count = 0

    for pl in payment_lines:
        res = matcher.match_payment(pl, open_invoices, retailers)
        matcher.apply_match_results(session, pl, res, actor_id=user.id)
        if res.status == "auto_confirmed":
            auto_count += 1
        elif res.status == "proposed":
            proposed_count += 1

    session.commit()

    msg = (
        f"Statement '{file.filename}' imported: {len(payment_lines)} lines ingested. "
        f"{auto_count} auto-confirmed, {proposed_count} routed to review queue."
    )
    return RedirectResponse(f"/?msg={msg}", status_code=303)


# ---------------------------------------------------------------------------
# Step 3: Match Approval & Review Queue Action Routes
# ---------------------------------------------------------------------------
@router.post("/matches/{match_id}/approve")
def approve_match(
    match_id: str,
    session: Session = Depends(get_session),
    context: tuple[Tenant, User] = Depends(get_current_tenant_and_user),
):
    tenant, user = context
    set_tenant(session, tenant.id)

    try:
        mid = uuid.UUID(match_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid match ID")

    pm = session.get(PaymentMatch, mid)
    if not pm or pm.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Match not found")

    pl = pm.payment_line
    # Rule 2: Evidence Ladder Guard
    try:
        evidence = EvidenceLevel(pl.evidence_level)
        assert_confirmable(evidence)
    except InsufficientEvidenceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    now = datetime.now(timezone.utc)
    pm.state = MatchState.CONFIRMED.value
    pm.confirmed_by = user.id
    pm.confirmed_at = now
    pl.state = PaymentState.CONFIRMED.value

    # Update invoice state if settled
    inv = pm.invoice
    confirmed_paid = session.execute(
        select(func.coalesce(func.sum(PaymentMatch.matched_santim), 0))
        .where(PaymentMatch.invoice_id == inv.id, PaymentMatch.state == MatchState.CONFIRMED.value)
    ).scalar_one()

    if confirmed_paid >= inv.amount_santim:
        inv.state = InvoiceState.SETTLED.value
    else:
        inv.state = InvoiceState.PARTIALLY_PAID.value

    session.commit()
    return HTMLResponse("<div class='p-3 text-xs font-bold text-emerald-800 bg-emerald-50 rounded-lg'>✓ Match Confirmed</div>")


@router.post("/matches/{match_id}/mark-unverified")
def mark_unverified(
    match_id: str,
    session: Session = Depends(get_session),
    context: tuple[Tenant, User] = Depends(get_current_tenant_and_user),
):
    """
    Owner override for L4 items (Rule 2).
    Sets payment to accepted_unverified, which is NEVER counted as paid in any total or report.
    """
    tenant, user = context
    if user.role != UserRole.OWNER.value:
        raise HTTPException(status_code=403, detail="Only owners can mark L4 items as accepted_unverified.")

    set_tenant(session, tenant.id)
    try:
        pid = uuid.UUID(match_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid ID")

    # Try lookup as match or payment line
    pl = session.get(PaymentLine, pid)
    if not pl:
        pm = session.get(PaymentMatch, pid)
        if pm:
            pl = pm.payment_line

    if not pl or pl.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Record not found")

    pl.state = PaymentState.ACCEPTED_UNVERIFIED.value
    session.commit()

    return HTMLResponse(
        "<div class='p-3 text-xs font-bold text-purple-900 bg-purple-50 rounded-lg'>"
        "✓ Marked Accepted Unverified (L4). Excluded from all paid reports."
        "</div>"
    )


@router.post("/matches/{match_id}/reject")
def reject_match(
    match_id: str,
    session: Session = Depends(get_session),
    context: tuple[Tenant, User] = Depends(get_current_tenant_and_user),
):
    tenant, user = context
    set_tenant(session, tenant.id)

    try:
        pid = uuid.UUID(match_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid ID")

    pm = session.get(PaymentMatch, pid)
    if pm:
        pm.state = MatchState.REJECTED.value
        pm.payment_line.state = PaymentState.REJECTED.value
    else:
        pl = session.get(PaymentLine, pid)
        if pl:
            pl.state = PaymentState.REJECTED.value

    session.commit()
    return HTMLResponse("<div class='p-3 text-xs font-bold text-slate-500 bg-slate-100 rounded-lg'>✕ Match Rejected</div>")


# ---------------------------------------------------------------------------
# Reports & Plain-Text Morning Brief
# ---------------------------------------------------------------------------
@router.get("/brief", response_class=PlainTextResponse)
def morning_brief(
    session: Session = Depends(get_session),
    context: tuple[Tenant, User] = Depends(get_current_tenant_and_user),
):
    tenant, _ = context
    set_tenant(session, tenant.id)
    text_content = generate_morning_brief_text(session, tenant_id=tenant.id)
    return PlainTextResponse(text_content)


# ---------------------------------------------------------------------------
# Exports (Full Data & Accountant)
# ---------------------------------------------------------------------------
@router.get("/exports/sales.csv")
def download_sales_csv(
    session: Session = Depends(get_session),
    context: tuple[Tenant, User] = Depends(get_current_tenant_and_user),
):
    tenant, _ = context
    set_tenant(session, tenant.id)
    content = export_sales_csv(session, tenant_id=tenant.id)
    return Response(
        content=content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=sales_ledger.csv"},
    )


@router.get("/exports/payments.csv")
def download_payments_csv(
    session: Session = Depends(get_session),
    context: tuple[Tenant, User] = Depends(get_current_tenant_and_user),
):
    tenant, _ = context
    set_tenant(session, tenant.id)
    content = export_payments_csv(session, tenant_id=tenant.id)
    return Response(
        content=content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=payments_ledger.csv"},
    )


@router.get("/exports/accountant.csv")
def download_accountant_csv(
    session: Session = Depends(get_session),
    context: tuple[Tenant, User] = Depends(get_current_tenant_and_user),
):
    tenant, _ = context
    set_tenant(session, tenant.id)
    content = export_accountant_csv(session, tenant_id=tenant.id)
    return Response(
        content=content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=partner_accountant_export.csv"},
    )


# ---------------------------------------------------------------------------
# Retailer Balance Confirmation & Token Routes (Feature D)
# ---------------------------------------------------------------------------
@router.post("/retailers/{retailer_id}/confirmation-token")
def generate_confirmation_token_endpoint(
    retailer_id: str,
    session: Session = Depends(get_session),
    context: tuple[Tenant, User] = Depends(get_current_tenant_and_user),
):
    tenant, user = context
    set_tenant(session, tenant.id)
    try:
        rid = uuid.UUID(retailer_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid retailer ID")

    retailer = session.get(Retailer, rid)
    if not retailer or retailer.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Retailer not found")

    token_rec, raw_token = create_retailer_confirmation_token(
        session,
        tenant_id=tenant.id,
        retailer_id=retailer.id,
        expires_in_days=7,
    )
    session.commit()

    link = f"/confirm-balance/{raw_token}"
    return {"token_id": str(token_rec.id), "raw_token": raw_token, "link": link}


@router.get(
    "/confirm-balance/{raw_token}",
    response_class=HTMLResponse,
    dependencies=[Depends(confirmation_rate_limiter)],
)
def view_balance_confirmation(
    request: Request,
    raw_token: str,
    session: Session = Depends(get_session),
):
    try:
        token_rec, retailer, invoices = validate_confirmation_token(session, raw_token=raw_token)
    except TokenError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    tenant = session.get(Tenant, token_rec.tenant_id)
    total_santim = sum(inv.amount_santim for inv in invoices)
    today = datetime.now(timezone.utc).date()

    return templates.TemplateResponse(
        request=request,
        name="confirm_balance.html",
        context={
            "tenant": tenant,
            "retailer": retailer,
            "invoices": invoices,
            "total_outstanding_etb": to_etb(total_santim),
            "as_of_date": today,
            "raw_token": raw_token,
        },
    )


@router.post(
    "/confirm-balance/{raw_token}/confirm",
    response_class=HTMLResponse,
    dependencies=[Depends(confirmation_rate_limiter)],
)
def submit_balance_confirmation(
    request: Request,
    raw_token: str,
    payment_ref: str = Form(None),
    payment_amount_etb: str = Form(None),
    payment_date: str = Form(None),
    session: Session = Depends(get_session),
):
    try:
        token_rec, retailer, _ = validate_confirmation_token(session, raw_token=raw_token)
    except TokenError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    tenant = session.get(Tenant, token_rec.tenant_id)
    set_tenant(session, token_rec.tenant_id)

    explicit_payments = []
    if payment_amount_etb and payment_amount_etb.strip():
        explicit_payments.append({
            "amount_etb": payment_amount_etb.strip(),
            "source_ref": payment_ref.strip() if payment_ref else None,
            "value_date": payment_date.strip() if payment_date else None,
        })

    _, created_l3 = confirm_retailer_balance(
        session,
        token_record=token_rec,
        explicit_payments=explicit_payments if explicit_payments else None,
    )
    session.commit()

    return templates.TemplateResponse(
        request=request,
        name="confirm_result.html",
        context={
            "title": "Confirmation Successful",
            "status": "confirmed",
            "tenant": tenant,
            "retailer": retailer,
            "l3_count": len(created_l3),
        },
    )


@router.post(
    "/confirm-balance/{raw_token}/dispute",
    response_class=HTMLResponse,
    dependencies=[Depends(confirmation_rate_limiter)],
)
def submit_balance_dispute(
    request: Request,
    raw_token: str,
    reason_code: str = Form(...),
    explanation: str = Form(...),
    session: Session = Depends(get_session),
):
    try:
        token_rec, retailer, _ = validate_confirmation_token(session, raw_token=raw_token)
    except TokenError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    tenant = session.get(Tenant, token_rec.tenant_id)
    set_tenant(session, token_rec.tenant_id)

    dispute_retailer_balance(
        session,
        token_record=token_rec,
        reason_code=reason_code,
        explanation=explanation,
    )
    session.commit()

    return templates.TemplateResponse(
        request=request,
        name="confirm_result.html",
        context={
            "title": "Dispute Submitted",
            "status": "disputed",
            "tenant": tenant,
            "retailer": retailer,
            "reason_code": reason_code,
            "explanation": explanation,
        },
    )
