"""
app/channels/web/routes.py

Web routes for Daily Close:
- Authentication (login, logout, logout-all) with Argon2id and DB session tokens
- Dashboard, 3 daily close steps, exports, and match review
- Staff user management (owner-only)
- Public retailer balance confirmation with persistent rate limiting
"""

from __future__ import annotations

import io
import uuid
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.channels.web.auth_deps import (
    SESSION_COOKIE_NAME,
    get_current_session_info,
    get_current_user_and_tenant,
    require_owner,
    verify_csrf,
)
from app.importers.base import parse_amount_santim, parse_statement_date
from app.importers.csv_importer import CsvStatementImporter
from app.importers.mapping import (
    CBE_CSV_CONFIG,
    DASHEN_CSV_CONFIG,
    TELEBIRR_XLSX_CONFIG,
    ColumnMappingConfig,
)
from app.importers.xlsx_importer import XlsxStatementImporter
from app.kernel.auth import (
    create_user_session,
    generate_csrf_token,
    hash_password,
    normalize_identifier,
    terminate_all_user_sessions,
    terminate_session,
    validate_session_token,
    verify_password,
)
from app.kernel.db import get_session
from app.kernel.enums import EvidenceLevel, InvoiceState, MatchState, PaymentState, UserRole
from app.kernel.events import append_event
from app.kernel.guards import InsufficientEvidenceError, assert_confirmable
from app.kernel.models import ImportBatch, Invoice, PaymentLine, PaymentMatch, Retailer, Session as SessionModel, Tenant, User
from app.kernel.money import to_etb
from app.kernel.persistent_rate_limit import (
    RateLimitExceededError,
    check_persistent_rate_limit,
    record_failed_login,
    record_successful_login,
)
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
from app.matching.normalization import normalize_phone
from app.reports.aging import generate_aging_report
from app.reports.exports import export_accountant_csv, export_payments_csv, export_sales_csv
from app.reports.morning_brief import generate_morning_brief_text
from app.settings import settings


TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# File upload security limits
MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB limit
ALLOWED_SALES_EXTENSIONS = {".csv"}
ALLOWED_STATEMENT_EXTENSIONS = {".csv", ".xlsx"}


def validate_uploaded_file(file: UploadFile, allowed_extensions: set[str]) -> str:
    """Validate filename extension and sanitize path."""
    raw_name = file.filename or ""
    safe_name = Path(raw_name).name  # Strips directory traversal (/ or \\)
    ext = Path(safe_name).suffix.lower()
    if not ext or ext not in allowed_extensions:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file extension '{ext}'. Allowed extensions: {', '.join(sorted(allowed_extensions))}",
        )
    return safe_name


def _get_client_ip(request: Request) -> str:
    """Extract client IP address for persistent rate limiting."""
    x_forwarded = request.headers.get("x-forwarded-for")
    if x_forwarded:
        return x_forwarded.split(",")[0].strip()
    return request.client.host if request.client else "127.0.0.1"


router = APIRouter()


# ---------------------------------------------------------------------------
# Authentication Routes (Web Login, Logout, Logout-Everywhere)
# ---------------------------------------------------------------------------
@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, session: Session = Depends(get_session)):
    """Render login form. Redirects to /dashboard if session already active."""
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if raw_token:
        try:
            validate_session_token(
                session,
                raw_token=raw_token,
                idle_timeout_minutes=settings.session_idle_timeout_minutes,
            )
            return RedirectResponse("/dashboard", status_code=303)
        except Exception:
            pass

    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": None},
    )


# Global dummy Argon2id hash for constant-time verification parity on unknown/locked accounts (SEC-12)
_DUMMY_ARGON2_HASH = "$argon2id$v=19$m=65536,t=3,p=4$dummyhashfordummyuserverificationtiming$dummyhashfordummyuserverificationtiming"


@router.post("/login")
def login_submit(
    request: Request,
    identifier: str = Form(...),
    password: str = Form(...),
    session: Session = Depends(get_session),
):
    """
    Authenticate user with email or phone + password (Argon2id).
    Enforces persistent IP rate limiting and 15-minute account lockout after 5 failures.
    Sets HttpOnly session cookie on success.
    """
    client_ip = _get_client_ip(request)

    # 1. IP-level persistent rate limiting (10 attempts / minute, 5m lockout)
    try:
        check_persistent_rate_limit(
            session,
            key=f"login:ip:{client_ip}",
            max_requests=10,
            window_seconds=60,
            lockout_seconds=300,
        )
    except RateLimitExceededError as exc:
        session.commit()
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": str(exc)},
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    # 2. Normalize identifier (email or phone)
    clean_id = normalize_identifier(identifier)
    now = datetime.now(timezone.utc)

    # 3. Look up user across tenants via SECURITY DEFINER function under app role (SEC-09)
    user_row = None
    if session.bind and session.bind.dialect.name == "postgresql":
        user_row = session.execute(
            text("SELECT id, tenant_id, role, display_name, email, phone, password_hash, is_active, failed_login_attempts, locked_until FROM lookup_user_by_identifier(:ident)"),
            {"ident": clean_id},
        ).mappings().first()
    else:
        u = session.execute(
            select(User).where(
                (func.lower(User.email) == clean_id.lower()) | (User.phone == clean_id)
            )
        ).scalars().first()
        if u:
            user_row = {
                "id": u.id,
                "tenant_id": u.tenant_id,
                "role": u.role,
                "display_name": u.display_name,
                "email": u.email,
                "phone": u.phone,
                "password_hash": u.password_hash,
                "is_active": u.is_active,
                "failed_login_attempts": u.failed_login_attempts,
                "locked_until": u.locked_until,
            }

    # Dummy Argon2id hash for constant-time verification parity on unknown/locked accounts (SEC-12)
    _DUMMY_ARGON2_HASH = "$argon2id$v=19$m=65536,t=3,p=4$dummyhashfordummyuserverificationtiming$dummyhashfordummyuserverificationtiming"

    # 4. Check if account exists, is active, and is not locked
    if not user_row or not user_row["is_active"]:
        verify_password(_DUMMY_ARGON2_HASH, password)
        record_failed_login(session, user=None, ip_address=client_ip, identifier=clean_id)
        session.commit()
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Invalid identifier or password."},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    # If locked, invoke dummy verify for timing parity and return identical 401 response
    if user_row["locked_until"] and user_row["locked_until"] > now:
        verify_password(_DUMMY_ARGON2_HASH, password)
        set_tenant(session, user_row["tenant_id"])
        user = session.get(User, user_row["id"])
        record_failed_login(session, user=user, ip_address=client_ip)
        session.commit()
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Invalid identifier or password."},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    # 5. Verify Argon2id password hash under tenant context
    set_tenant(session, user_row["tenant_id"])
    user = session.get(User, user_row["id"])

    if not user or not verify_password(user.password_hash, password):
        record_failed_login(session, user=user, ip_address=client_ip)
        session.commit()
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Invalid identifier or password."},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    # 6. Login Success: reset counter, issue opaque session token
    record_successful_login(session, user=user, ip_address=client_ip)
    session_rec, raw_token = create_user_session(
        session,
        user=user,
        ip_address=client_ip,
        user_agent=request.headers.get("user-agent"),
        lifetime_hours=settings.session_lifetime_hours,
    )
    session.commit()

    resp = RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    resp.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=raw_token,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        max_age=settings.session_lifetime_hours * 3600,
        path="/",
    )
    return resp


@router.post("/logout")
def logout(
    request: Request,
    session: Session = Depends(get_session),
    session_info: tuple[Tenant, User, SessionModel] = Depends(get_current_session_info),
    _csrf: None = Depends(verify_csrf),
):
    """Log out current session and clear cookie."""
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw_token:
        auth_hdr = request.headers.get("Authorization")
        if auth_hdr and auth_hdr.startswith("Bearer "):
            raw_token = auth_hdr[7:].strip()

    if raw_token:
        terminate_session(session, raw_token=raw_token)
        session.commit()

    resp = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    resp.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return resp


@router.post("/logout-all")
def logout_all(
    request: Request,
    session: Session = Depends(get_session),
    session_info: tuple[Tenant, User, SessionModel] = Depends(get_current_session_info),
    _csrf: None = Depends(verify_csrf),
):
    """Log out everywhere: revoke all active sessions for current user."""
    _, user, _ = session_info
    terminate_all_user_sessions(session, user_id=user.id)
    session.commit()

    resp = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    resp.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return resp


# ---------------------------------------------------------------------------
# Staff User Management Routes (Owner Only)
# ---------------------------------------------------------------------------
@router.get("/settings/users", response_class=HTMLResponse)
def list_users(
    request: Request,
    session: Session = Depends(get_session),
    session_info: tuple[Tenant, User, SessionModel] = Depends(get_current_session_info),
    msg: str | None = Query(None),
):
    """List staff accounts for current tenant."""
    tenant, user, session_rec = session_info
    if user.role != UserRole.OWNER.value:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden: Owner role required.")

    set_tenant(session, tenant.id)
    users = session.execute(
        select(User).where(User.tenant_id == tenant.id).order_by(User.created_at)
    ).scalars().all()

    csrf_token = generate_csrf_token(session_rec.id, settings.secret_key)
    return templates.TemplateResponse(
        request=request,
        name="user_management.html",
        context={
            "tenant_name": tenant.name,
            "user_role": user.role,
            "users": users,
            "csrf_token": csrf_token,
            "message": msg,
        },
    )


@router.post("/settings/users")
def create_staff_user(
    request: Request,
    display_name: str = Form(...),
    role: str = Form(...),
    password: str = Form(...),
    email: str | None = Form(None),
    phone: str | None = Form(None),
    session: Session = Depends(get_session),
    session_info: tuple[Tenant, User, SessionModel] = Depends(get_current_session_info),
    _csrf: None = Depends(verify_csrf),
):
    """Provision a new staff account under the owner's tenant."""
    tenant, actor_user, _ = session_info
    if actor_user.role != UserRole.OWNER.value:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden: Owner role required.")

    if role not in (UserRole.OWNER.value, UserRole.CASHIER.value):
        raise HTTPException(status_code=400, detail="Invalid role. Must be owner or cashier.")

    if len(password) < 10:
        raise HTTPException(status_code=400, detail="Password must be at least 10 characters long.")

    clean_email = email.strip().lower() if email and email.strip() else None
    clean_phone = normalize_phone(phone.strip()) if phone and phone.strip() else None

    if not clean_email and not clean_phone:
        raise HTTPException(status_code=400, detail="Either email or phone is required.")

    set_tenant(session, tenant.id)

    # Check for duplicate identifier
    existing = session.execute(
        select(User).where(
            (User.tenant_id == tenant.id) & (
                ((func.lower(User.email) == clean_email) & (clean_email.isnot(None))) |
                ((User.phone == clean_phone) & (clean_phone.isnot(None)))
            )
        )
    ).scalars().first()
    if existing:
        raise HTTPException(status_code=400, detail="A user with this email or phone already exists in this tenant.")

    pwd_hash = hash_password(password)
    now = datetime.now(timezone.utc)
    new_user = User(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        role=role,
        display_name=display_name.strip(),
        email=clean_email,
        phone=clean_phone,
        password_hash=pwd_hash,
        is_active=True,
        created_at=now,
    )
    session.add(new_user)
    session.flush()

    append_event(
        session,
        tenant_id=tenant.id,
        event_type="auth.user_created",
        aggregate_type="user",
        aggregate_id=new_user.id,
        actor_id=actor_user.id,
        payload={"role": role, "is_active": True},
    )
    session.commit()

    return RedirectResponse("/settings/users?msg=User+account+created+successfully", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/settings/users/{user_id}/reset-password")
def reset_staff_password(
    user_id: str,
    new_password: str = Form(...),
    session: Session = Depends(get_session),
    session_info: tuple[Tenant, User, SessionModel] = Depends(get_current_session_info),
    _csrf: None = Depends(verify_csrf),
):
    """Owner reset of staff user password with immediate session revocation."""
    tenant, actor_user, _ = session_info
    if actor_user.role != UserRole.OWNER.value:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden: Owner role required.")

    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user ID.")

    if len(new_password) < 10:
        raise HTTPException(status_code=400, detail="New password must be at least 10 characters long.")

    set_tenant(session, tenant.id)
    target_user = session.get(User, uid)
    if not target_user or target_user.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="User not found.")

    target_user.password_hash = hash_password(new_password)
    target_user.failed_login_attempts = 0
    target_user.locked_until = None
    terminate_all_user_sessions(session, user_id=target_user.id)

    append_event(
        session,
        tenant_id=tenant.id,
        event_type="auth.password_reset",
        aggregate_type="user",
        aggregate_id=target_user.id,
        actor_id=actor_user.id,
        payload={},
    )
    session.commit()

    return RedirectResponse("/settings/users?msg=Password+reset+successfully", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/settings/users/{user_id}/deactivate")
def deactivate_staff_user(
    user_id: str,
    session: Session = Depends(get_session),
    session_info: tuple[Tenant, User, SessionModel] = Depends(get_current_session_info),
    _csrf: None = Depends(verify_csrf),
):
    """Deactivate a staff user account and terminate all their active sessions immediately."""
    tenant, actor_user, _ = session_info
    if actor_user.role != UserRole.OWNER.value:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden: Owner role required.")

    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user ID.")

    if uid == actor_user.id:
        raise HTTPException(status_code=400, detail="Cannot deactivate your own account.")

    set_tenant(session, tenant.id)
    target_user = session.get(User, uid)
    if not target_user or target_user.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="User not found.")

    target_user.is_active = False
    terminate_all_user_sessions(session, user_id=target_user.id)

    append_event(
        session,
        tenant_id=tenant.id,
        event_type="auth.user_deactivated",
        aggregate_type="user",
        aggregate_id=target_user.id,
        actor_id=actor_user.id,
        payload={},
    )
    session.commit()

    return RedirectResponse("/settings/users?msg=User+deactivated+successfully", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Dashboard View
# ---------------------------------------------------------------------------
@router.get("/", response_class=HTMLResponse)
@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    session: Session = Depends(get_session),
    session_info: tuple[Tenant, User, SessionModel] = Depends(get_current_session_info),
    msg: str | None = Query(None),
):
    tenant, user, session_rec = session_info
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

    csrf_token = generate_csrf_token(session_rec.id, settings.secret_key)

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
            "csrf_token": csrf_token,
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
    session_info: tuple[Tenant, User, SessionModel] = Depends(get_current_session_info),
    _csrf: None = Depends(verify_csrf),
):
    tenant, user, _ = session_info
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
    session_info: tuple[Tenant, User, SessionModel] = Depends(get_current_session_info),
    _csrf: None = Depends(verify_csrf),
):
    tenant, user, _ = session_info
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
# Step 2: Statement Import & Auto-Matching Route (Owner Only)
# ---------------------------------------------------------------------------
@router.post("/statements/upload")
async def upload_statement(
    source_name: str = Form(...),
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    owner_context: tuple[Tenant, User] = Depends(require_owner),
    _csrf: None = Depends(verify_csrf),
):
    tenant, user = owner_context
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
# Step 3: Match Approval & Review Queue Action Routes (Owner Only)
# ---------------------------------------------------------------------------
@router.post("/matches/{match_id}/approve")
def approve_match(
    match_id: str,
    session: Session = Depends(get_session),
    owner_context: tuple[Tenant, User] = Depends(require_owner),
    _csrf: None = Depends(verify_csrf),
):
    tenant, user = owner_context
    set_tenant(session, tenant.id)

    try:
        mid = uuid.UUID(match_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid match ID")

    # 1. Lock PaymentMatch in transaction (SEC-02)
    pm = session.execute(
        select(PaymentMatch)
        .where(PaymentMatch.id == mid, PaymentMatch.tenant_id == tenant.id)
        .with_for_update()
    ).scalar_one_or_none()

    if not pm:
        raise HTTPException(status_code=404, detail="Match not found")

    # Idempotency: if already confirmed, return success immediately
    if pm.state == MatchState.CONFIRMED.value:
        return HTMLResponse("<div class='p-3 text-xs font-bold text-emerald-800 bg-emerald-50 rounded-lg'>✓ Match Confirmed</div>")

    # 2. Lock PaymentLine in consistent order (SEC-02)
    pl = session.execute(
        select(PaymentLine)
        .where(PaymentLine.id == pm.payment_line_id, PaymentLine.tenant_id == tenant.id)
        .with_for_update()
    ).scalar_one()

    # Rule 2: Evidence Ladder Guard
    try:
        evidence = EvidenceLevel(pl.evidence_level)
        assert_confirmable(evidence)
    except InsufficientEvidenceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Check cumulative confirmed allocations on payment line
    allocated_santim = session.execute(
        select(func.coalesce(func.sum(PaymentMatch.matched_santim), 0))
        .where(
            PaymentMatch.payment_line_id == pl.id,
            PaymentMatch.state == MatchState.CONFIRMED.value,
            PaymentMatch.id != pm.id,
        )
    ).scalar_one()

    if allocated_santim + pm.matched_santim > pl.amount_santim:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Payment line is already confirmed or fully allocated ({allocated_santim}/{pl.amount_santim} santim).",
        )

    # 3. Lock Invoice in consistent order (SEC-02)
    inv = session.execute(
        select(Invoice)
        .where(Invoice.id == pm.invoice_id, Invoice.tenant_id == tenant.id)
        .with_for_update()
    ).scalar_one()

    inv_paid = session.execute(
        select(func.coalesce(func.sum(PaymentMatch.matched_santim), 0))
        .where(
            PaymentMatch.invoice_id == inv.id,
            PaymentMatch.state == MatchState.CONFIRMED.value,
            PaymentMatch.id != pm.id,
        )
    ).scalar_one()

    if inv_paid >= inv.amount_santim:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Invoice {inv.invoice_number} is already fully settled ({inv_paid}/{inv.amount_santim} santim).",
        )

    now = datetime.now(timezone.utc)
    pm.state = MatchState.CONFIRMED.value
    pm.confirmed_by = user.id
    pm.confirmed_at = now

    if allocated_santim + pm.matched_santim >= pl.amount_santim:
        pl.state = PaymentState.CONFIRMED.value
    else:
        pl.state = PaymentState.PARTIALLY_MATCHED.value

    # Update invoice state
    new_inv_paid = inv_paid + pm.matched_santim
    if new_inv_paid >= inv.amount_santim:
        inv.state = InvoiceState.SETTLED.value
    else:
        inv.state = InvoiceState.PARTIALLY_PAID.value

    session.commit()
    return HTMLResponse("<div class='p-3 text-xs font-bold text-emerald-800 bg-emerald-50 rounded-lg'>✓ Match Confirmed</div>")


@router.post("/matches/{match_id}/mark-unverified")
def mark_unverified(
    match_id: str,
    session: Session = Depends(get_session),
    owner_context: tuple[Tenant, User] = Depends(require_owner),
    _csrf: None = Depends(verify_csrf),
):
    """
    Owner override for L4 items (Rule 2).
    Sets payment to accepted_unverified, which is NEVER counted as paid in any total or report.
    """
    tenant, user = owner_context
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
    owner_context: tuple[Tenant, User] = Depends(require_owner),
    _csrf: None = Depends(verify_csrf),
):
    tenant, user = owner_context
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
    session_info: tuple[Tenant, User, SessionModel] = Depends(get_current_session_info),
):
    tenant, _, _ = session_info
    set_tenant(session, tenant.id)
    text_content = generate_morning_brief_text(session, tenant_id=tenant.id)
    return PlainTextResponse(text_content)


# ---------------------------------------------------------------------------
# Exports (Full Data & Accountant) — Owner Only
# ---------------------------------------------------------------------------
@router.get("/exports/sales.csv")
def download_sales_csv(
    session: Session = Depends(get_session),
    owner_context: tuple[Tenant, User] = Depends(require_owner),
):
    tenant, _ = owner_context
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
    owner_context: tuple[Tenant, User] = Depends(require_owner),
):
    tenant, _ = owner_context
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
    owner_context: tuple[Tenant, User] = Depends(require_owner),
):
    tenant, _ = owner_context
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
    session_info: tuple[Tenant, User, SessionModel] = Depends(get_current_session_info),
    _csrf: None = Depends(verify_csrf),
):
    tenant, user, _ = session_info
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
)
def view_balance_confirmation(
    request: Request,
    raw_token: str,
    session: Session = Depends(get_session),
):
    """
    Public retailer confirmation view.
    Rate limited in PostgreSQL by client IP.
    Strictly ignores any existing session cookies; tenant is resolved only from signed token.
    """
    client_ip = _get_client_ip(request)
    try:
        check_persistent_rate_limit(
            session,
            key=f"confirm:ip:{client_ip}",
            max_requests=15,
            window_seconds=60,
        )
    except RateLimitExceededError as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc))

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
)
def submit_balance_confirmation(
    request: Request,
    raw_token: str,
    payment_ref: str = Form(None),
    payment_amount_etb: str = Form(None),
    payment_date: str = Form(None),
    session: Session = Depends(get_session),
):
    """
    Public confirmation submission.
    Rate limited in PostgreSQL by client IP.
    Single-use token verification establishes tenant context.
    """
    client_ip = _get_client_ip(request)
    try:
        check_persistent_rate_limit(
            session,
            key=f"confirm:ip:{client_ip}",
            max_requests=15,
            window_seconds=60,
        )
    except RateLimitExceededError as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc))

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
)
def submit_balance_dispute(
    request: Request,
    raw_token: str,
    reason_code: str = Form(...),
    explanation: str = Form(...),
    session: Session = Depends(get_session),
):
    """
    Public dispute submission.
    Rate limited in PostgreSQL by client IP.
    Single-use token verification establishes tenant context.
    """
    client_ip = _get_client_ip(request)
    try:
        check_persistent_rate_limit(
            session,
            key=f"confirm:ip:{client_ip}",
            max_requests=15,
            window_seconds=60,
        )
    except RateLimitExceededError as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc))

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
