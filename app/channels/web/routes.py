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
from urllib.parse import urlparse, quote

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, distinct, func, select, text
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
from app.kernel.enums import EvidenceLevel, InvoiceState, MatchState, PaymentState, ReversalReasonCode, UserRole
from app.kernel.events import append_event
from app.kernel.guards import InsufficientEvidenceError, assert_confirmable
from app.kernel.models import Event, ImportBatch, Invoice, PaymentLine, PaymentMatch, ProcessedApprovalToken, RateLimitBucket, Retailer, ReversalNote, Session as SessionModel, Tenant, User
from app.channels.telegram.i18n import t
from app.kernel.money import format_money, to_etb
from app.matching.context import compute_match_reconciliation_context
from app.kernel.tokens import (
    generate_match_approval_token,
    generate_password_reset_token,
    hash_approval_token,
    verify_match_approval_token,
    verify_password_reset_token,
)
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
from app.reports.exports import export_accountant_csv, export_payments_csv, export_reversals_csv, export_sales_csv
from app.reports.morning_brief import generate_morning_brief_text
from app.settings import settings


TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["format_money"] = format_money


def format_match_error_response(
    request: Request,
    locale_key: str,
    status_code: int = 400,
    detail: str = "",
    match_id: str | uuid.UUID | None = None,
) -> Response:
    lang = request.cookies.get("lang", "en")
    msg = t(locale_key, lang=lang) if locale_key else detail
    if not msg or msg == locale_key:
        msg = detail or "An error occurred."
    if request.headers.get("hx-request") or "text/html" in request.headers.get("accept", ""):
        headers = {}
        target_card = str(match_id) if match_id else None
        if target_card:
            headers["HX-Retarget"] = f"#match-card-alert-{target_card}"
            headers["HX-Reswap"] = "innerHTML"
        return HTMLResponse(
            f"<div data-testid='match-error-alert' class='p-3 mb-3 text-xs font-bold text-rose-800 bg-rose-50 border border-rose-200 rounded-lg'>✕ {msg}</div>",
            status_code=200,
            headers=headers,
        )
    raise HTTPException(status_code=status_code, detail=detail or msg)


def get_pending_review_queue_count(session: Session, tenant_id: uuid.UUID) -> int:
    """
    Compute total items awaiting review in the review queue.
    Strictly consistent with dashboard items (one card per payment line in proposed or unmatched state).
    """
    return session.execute(
        select(func.count(distinct(PaymentLine.id)))
        .where(
            PaymentLine.tenant_id == tenant_id,
            PaymentLine.state.in_([PaymentState.PROPOSED.value, PaymentState.UNMATCHED.value]),
        )
    ).scalar_one()

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
    """
    Extract client IP address for persistent rate limiting.
    SEC-05: Trust X-Real-IP and X-Forwarded-For ONLY when the immediate
    peer address (request.client.host) is in configured settings.trusted_proxies
    (default ["127.0.0.1"]). Otherwise, use socket address.
    """
    peer_ip = "127.0.0.1"
    if getattr(request, "client", None) and request.client.host:
        peer_ip = request.client.host

    trusted_list = getattr(settings, "trusted_proxies", ["127.0.0.1", "::1"])
    if peer_ip in trusted_list:
        x_real_ip = request.headers.get("x-real-ip")
        if x_real_ip:
            return x_real_ip.strip()

        x_forwarded = request.headers.get("x-forwarded-for")
        if x_forwarded:
            ips = [ip.strip() for ip in x_forwarded.split(",") if ip.strip()]
            if ips:
                return ips[-1]

    return peer_ip


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

    lang = request.cookies.get("lang", "en")
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": None, "lang": lang},
    )


@router.get("/set-lang/{lang_code}")
def set_language_endpoint(lang_code: str, request: Request):
    code = lang_code.lower()
    if code not in ("en", "am"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid language code. Supported languages are 'en' and 'am'.",
        )
    referer = request.headers.get("referer", "/dashboard")
    target_url = "/dashboard"
    if referer:
        try:
            parsed = urlparse(referer)
            # Accept only local relative paths starting with '/' (and not '//')
            if not parsed.netloc and parsed.path.startswith("/") and not parsed.path.startswith("//"):
                target_url = parsed.path + (f"?{parsed.query}" if parsed.query else "")
            elif parsed.netloc:
                req_parsed = urlparse(str(request.base_url))
                if parsed.netloc == req_parsed.netloc and parsed.scheme == req_parsed.scheme:
                    target_url = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        except Exception:
            target_url = "/dashboard"

    response = RedirectResponse(url=target_url, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(key="lang", value=code, max_age=365 * 24 * 3600, path="/", samesite="lax")
    return response


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
    lang = request.cookies.get("lang", "en")

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
            context={"error": str(exc), "lang": lang},
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
            context={"error": "Invalid identifier or password.", "lang": lang},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    # 4b. Check if account or (account, IP) pair is locked / delayed (SEC-05)
    user_ip_key = f"login:account_ip:{user_row['id']}:{client_ip}"
    user_ip_bucket = session.get(RateLimitBucket, user_ip_key)

    is_current_ip_locked = bool(user_ip_bucket and user_ip_bucket.locked_until and user_ip_bucket.locked_until > now)
    is_account_locked = bool(user_row["locked_until"] and user_row["locked_until"] > now)

    # If the user is locked, only block this request if the current IP was locked or account was admin-locked
    if is_current_ip_locked or (is_account_locked and (user_ip_bucket is not None or user_row["role"] != "owner")):
        verify_password(_DUMMY_ARGON2_HASH, password)
        set_tenant(session, user_row["tenant_id"])
        user = session.get(User, user_row["id"])
        record_failed_login(session, user=user, ip_address=client_ip)
        session.commit()
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Invalid identifier or password.", "lang": lang},
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
            context={"error": "Invalid identifier or password.", "lang": lang},
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
    reset_link: str | None = Query(None),
    target_name: str | None = Query(None),
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
            "reset_link": reset_link,
            "target_name": target_name,
            "current_user_id": user.id,
            "lang": request.cookies.get("lang", "en"),
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
    new_password: str | None = Form(None),
    session: Session = Depends(get_session),
    session_info: tuple[Tenant, User, SessionModel] = Depends(get_current_session_info),
    _csrf: None = Depends(verify_csrf),
):
    """
    Owner generation of single-use 24-hour password reset link.
    The owner never sees or chooses the password.
    """
    tenant, actor_user, _ = session_info
    if actor_user.role != UserRole.OWNER.value:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden: Owner role required.")

    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user ID.")

    set_tenant(session, tenant.id)
    target_user = session.get(User, uid)
    if not target_user or target_user.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="User not found.")

    token = generate_password_reset_token(
        settings.secret_key,
        tenant_id=tenant.id,
        user_id=target_user.id,
    )
    reset_link = f"/reset-password/{token}"

    append_event(
        session,
        tenant_id=tenant.id,
        event_type="auth.password_reset_link_generated",
        aggregate_type="user",
        aggregate_id=target_user.id,
        actor_id=actor_user.id,
        payload={"expires_in_hours": 24},
    )
    session.commit()

    encoded_link = quote(reset_link)
    encoded_name = quote(target_user.display_name)
    return RedirectResponse(
        f"/settings/users?reset_link={encoded_link}&target_name={encoded_name}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/reset-password/{token}", response_class=HTMLResponse)
def show_reset_password_page(token: str, request: Request, session: Session = Depends(get_session)):
    """Render single-use password reset page for staff member."""
    is_valid, err_msg, data = verify_password_reset_token(settings.secret_key, token)
    if not is_valid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=err_msg)

    token_sig = data.get("token_sig")
    existing_event = session.execute(
        select(Event).where(
            Event.tenant_id == data["tenant_id"],
            Event.event_type == "auth.password_reset_completed",
            Event.payload["token_sig"].astext == token_sig,
        )
    ).scalars().first()
    if existing_event:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This password reset link has already been used.")

    user = session.get(User, data["user_id"])
    if not user or not user.is_active or user.tenant_id != data["tenant_id"]:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User account not found or deactivated.")

    lang = request.cookies.get("lang", "en")
    csrf_token = generate_csrf_token(uuid.uuid4(), settings.secret_key)
    return templates.TemplateResponse(
        request=request,
        name="reset_password.html",
        context={
            "token": token,
            "display_name": user.display_name,
            "csrf_token": csrf_token,
            "lang": lang,
            "error": None,
        },
    )


@router.post("/reset-password/{token}")
def submit_reset_password(
    token: str,
    request: Request,
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    session: Session = Depends(get_session),
):
    """Process single-use password reset form submission."""
    is_valid, err_msg, data = verify_password_reset_token(settings.secret_key, token)
    if not is_valid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=err_msg)

    token_sig = data.get("token_sig")
    token_nonce = data.get("nonce")
    existing_event = session.execute(
        select(Event).where(
            Event.tenant_id == data["tenant_id"],
            Event.event_type == "auth.password_reset_completed",
            Event.payload["token_sig"].astext == token_sig,
        )
    ).scalars().first()
    if existing_event:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This password reset link has already been used.")

    user = session.get(User, data["user_id"])
    if not user or not user.is_active or user.tenant_id != data["tenant_id"]:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User account not found or deactivated.")

    lang = request.cookies.get("lang", "en")
    csrf_token = generate_csrf_token(uuid.uuid4(), settings.secret_key)
    if len(new_password) < 10:
        return templates.TemplateResponse(
            request=request,
            name="reset_password.html",
            context={
                "token": token,
                "display_name": user.display_name,
                "csrf_token": csrf_token,
                "lang": lang,
                "error": "Password must be at least 10 characters long.",
            },
            status_code=400,
        )

    if new_password != confirm_password:
        return templates.TemplateResponse(
            request=request,
            name="reset_password.html",
            context={
                "token": token,
                "display_name": user.display_name,
                "csrf_token": csrf_token,
                "lang": lang,
                "error": "Passwords do not match.",
            },
            status_code=400,
        )

    set_tenant(session, user.tenant_id)
    user.password_hash = hash_password(new_password)
    user.failed_login_attempts = 0
    user.locked_until = None
    terminate_all_user_sessions(session, user_id=user.id)

    append_event(
        session,
        tenant_id=user.tenant_id,
        event_type="auth.password_reset_completed",
        aggregate_type="user",
        aggregate_id=user.id,
        actor_id=user.id,
        payload={"token_sig": token_sig, "token_nonce": token_nonce},
    )
    session.commit()

    return RedirectResponse(
        url="/login?msg=Password+reset+successful.+Please+sign+in+with+your+new+password.",
        status_code=status.HTTP_303_SEE_OTHER,
    )


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
        target_inv = pm.invoice if pm else None

        inv_open_balance_santim = 0
        diff_santim = 0
        diff_etb = "0.00"
        match_kind = "exact"
        unallocated_credit_santim = 0
        unallocated_credit_etb = "0.00"
        diff_raw_etb = "0.00"
        unallocated_credit_raw_etb = "0.00"
        is_differing = False
        approval_token = ""
        alt_candidates = []

        if pm and target_inv:
            target_invs = target_inv.invoice_number
            ctx = compute_match_reconciliation_context(
                session,
                tenant_id=tenant.id,
                payment_match=pm,
                payment_line=pl,
                invoice=target_inv,
            )
            inv_open_balance_santim = ctx.invoice_open_santim
            diff_santim = ctx.diff_santim
            diff_etb = format_money(diff_santim, include_currency=True)
            match_kind = ctx.match_kind
            unallocated_credit_santim = ctx.unallocated_credit_santim
            unallocated_credit_etb = format_money(unallocated_credit_santim, include_currency=True)
            diff_raw_etb = to_etb(diff_santim)
            unallocated_credit_raw_etb = to_etb(unallocated_credit_santim)
            is_differing = ctx.is_differing

            # Generate HMAC-signed stale-view token with tenant and user scope
            approval_token = generate_match_approval_token(
                settings.secret_key,
                tenant_id=tenant.id,
                user_id=user.id,
                match_id=pm.id,
                matched_santim=ctx.matched_santim,
                invoice_amount_santim=ctx.invoice_total_santim,
                payment_amount_santim=ctx.payment_amount_santim,
                match_state=pm.state,
            )

            # Query alternative candidate open invoices: prioritize same-retailer first
            alt_rows = session.execute(
                select(Invoice)
                .where(
                    Invoice.tenant_id == tenant.id,
                    Invoice.state.in_([InvoiceState.OPEN.value, InvoiceState.PARTIALLY_PAID.value]),
                    Invoice.id != target_inv.id,
                )
                .order_by(
                    case((Invoice.retailer_id == target_inv.retailer_id, 0), else_=1).asc(),
                    func.abs(Invoice.amount_santim - pl.amount_santim).asc(),
                    Invoice.issued_date.desc(),
                )
                .limit(3)
            ).scalars().all()

            for alt_inv in alt_rows:
                alt_ret_name = alt_inv.retailer.name if alt_inv.retailer else "Unknown"
                is_same = (alt_inv.retailer_id == target_inv.retailer_id)
                alt_candidates.append({
                    "invoice_number": alt_inv.invoice_number,
                    "retailer_name": alt_ret_name,
                    "amount_etb": format_money(alt_inv.amount_santim, include_currency=True),
                    "due_date": alt_inv.due_date.strftime("%d/%m/%Y") if alt_inv.due_date else "N/A",
                    "is_same_retailer": is_same,
                    "retailer_label": "same retailer" if is_same else "different retailer",
                })

            # Plain-language "Why it matched" built from structured fields (No AI)
            ret_display = target_inv.retailer.name if target_inv.retailer else "Unknown"
            if pm.tier == "tier_a":
                if is_differing:
                    reason = f"Suggested match, please check: invoice '{target_inv.invoice_number}' referenced with {match_kind} amount"
                else:
                    reason = f"Suggested match, please check: invoice number '{target_inv.invoice_number}' referenced in payment line with exact amount match"
            elif pm.tier == "tier_b":
                reason = f"Suggested match, please check: counterparty account/phone matched for retailer '{ret_display}'"
            else:
                reason = f"Suggested match, please check: candidate open invoice for retailer '{ret_display}' based on name and amount similarity"
        elif pl.state == "proposed":
            reason = "Suggested match, please check: unverified payment awaiting review against open invoices"

        proposed_items.append({
            "payment_id": str(pl.id),
            "match_id": match_id,
            "amount_etb": format_money(pl.amount_santim, include_currency=True),
            "amount_santim": pl.amount_santim,
            "value_date": pl.value_date.strftime("%d/%m/%Y"),
            "evidence_level": pl.evidence_level,
            "counterparty_name": pl.counterparty_name,
            "counterparty_account": pl.counterparty_account,
            "raw_reference": pl.raw_reference,
            "target_invoice_numbers": target_invs,
            "target_invoice": target_inv,
            "invoice_amount_etb": format_money(target_inv.amount_santim, include_currency=True) if target_inv else "0.00 ETB",
            "invoice_issued_date": target_inv.issued_date.strftime("%d/%m/%Y") if target_inv and target_inv.issued_date else "N/A",
            "invoice_due_date": target_inv.due_date.strftime("%d/%m/%Y") if target_inv else "N/A",
            "retailer_name": target_inv.retailer.name if target_inv and target_inv.retailer else (pl.counterparty_name or "Unknown"),
            "retailer_phone": target_inv.retailer.phone if target_inv and target_inv.retailer else "N/A",
            "reason": reason,
            "is_ambiguous": is_ambiguous,
            "is_differing": is_differing,
            "diff_etb": diff_etb,
            "diff_raw_etb": diff_raw_etb,
            "match_kind": match_kind,
            "unallocated_credit_etb": unallocated_credit_etb,
            "unallocated_credit_raw_etb": unallocated_credit_raw_etb,
            "unallocated_credit_santim": unallocated_credit_santim,
            "approval_token": approval_token,
            "alternative_candidates": alt_candidates,
            "is_previously_reversed": getattr(pl, "is_previously_reversed", False),
            "relied_on_l3": (pl.evidence_level == "L3"),
        })

    csrf_token = generate_csrf_token(session_rec.id, settings.secret_key)
    lang = request.cookies.get("lang", "en")

    # Daily checklist counts
    today_sales_count = session.execute(
        select(func.count(Invoice.id)).where(
            Invoice.tenant_id == tenant.id,
            Invoice.issued_date == today,
        )
    ).scalar_one()

    today_batches_count = session.execute(
        select(func.count(ImportBatch.id)).where(
            ImportBatch.tenant_id == tenant.id,
            func.date(ImportBatch.imported_at) == today,
        )
    ).scalar_one()

    all_tasks_complete = (today_sales_count > 0 and today_batches_count > 0 and len(proposed_items) == 0)

    # Recently confirmed matches with Reverse
    recent_confirmed_rows = session.execute(
        select(PaymentMatch, PaymentLine, Invoice, Retailer)
        .join(PaymentLine, PaymentMatch.payment_line_id == PaymentLine.id)
        .join(Invoice, PaymentMatch.invoice_id == Invoice.id)
        .join(Retailer, Invoice.retailer_id == Retailer.id)
        .where(
            PaymentMatch.tenant_id == tenant.id,
            PaymentMatch.state == MatchState.CONFIRMED.value,
        )
        .order_by(PaymentMatch.created_at.desc())
        .limit(5)
    ).all()

    recently_confirmed = [
        {
            "match_id": str(pm.id),
            "retailer_name": ret.name,
            "invoice_number": inv.invoice_number,
            "matched_etb": format_money(pm.matched_santim),
            "confirmed_at": pm.created_at.strftime("%d/%m/%Y %H:%M"),
        }
        for pm, pl, inv, ret in recent_confirmed_rows
    ]

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
            "is_demo": getattr(tenant, "is_demo", False),
            "lang": lang,
            "today_sales_count": today_sales_count,
            "today_batches_count": today_batches_count,
            "all_tasks_complete": all_tasks_complete,
            "recently_confirmed": recently_confirmed,
        },
    )


@router.post("/demo/reset")
def handle_demo_reset(
    session: Session = Depends(get_session),
    owner_context: tuple[Tenant, User] = Depends(require_owner),
    _csrf: None = Depends(verify_csrf),
):
    """Safely reset a demo tenant back to baseline state."""
    from app.demo.reset import reset_demo_tenant

    tenant, user = owner_context
    if not getattr(tenant, "is_demo", False):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot reset a non-demo tenant.",
        )

    reset_demo_tenant(session, tenant.id)
    return RedirectResponse("/?msg=Demo+data+successfully+reset+to+baseline", status_code=status.HTTP_303_SEE_OTHER)


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

    safe_filename = validate_uploaded_file(file, ALLOWED_SALES_EXTENSIONS)
    content = await file.read(MAX_UPLOAD_SIZE + 1)
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail="File too large. Maximum allowed size is 10MB.")
    if not content or len(content.strip()) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if b"\x00" in content:
        raise HTTPException(status_code=400, detail="Uploaded CSV file contains invalid null bytes.")

    try:
        created, gaps, errors = parse_and_record_sales_csv(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            csv_content=content,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
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
    if not safe_filename.endswith(".xlsx") and b"\x00" in content:
        raise HTTPException(status_code=400, detail="Uploaded statement CSV file contains invalid null bytes.")

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
        # Previously reversed payments can never auto-confirm
        if getattr(pl, "is_previously_reversed", False) and res.status == "auto_confirmed":
            res.status = "proposed"
            res.reason += " (demoted to review queue: previously reversed payment)"
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
    request: Request,
    approval_token: str = Form(""),
    confirm_differing: bool = Form(False),
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

    # Check idempotent retry table and state guards
    if approval_token:
        tok_hash = hash_approval_token(approval_token)
        processed_tok = session.execute(
            select(ProcessedApprovalToken)
            .where(
                ProcessedApprovalToken.tenant_id == tenant.id,
                ProcessedApprovalToken.token_hash == tok_hash,
                ProcessedApprovalToken.payment_match_id == pm.id,
            )
        ).scalar_one_or_none()

        # Exact retry returns original success immediately
        if processed_tok and pm.state == MatchState.CONFIRMED.value:
            return HTMLResponse("<div class='p-3 text-xs font-bold text-emerald-800 bg-emerald-50 rounded-lg'>✓ Match Confirmed</div>")

        # Different token on already confirmed match or reversed match -> 409
        if pm.state == MatchState.CONFIRMED.value:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Match is already confirmed.",
            )
        if pm.state == MatchState.REVERSED.value:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A reversed match can never be re-approved.",
            )
    else:
        if pm.state == MatchState.REVERSED.value:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A reversed match can never be re-approved.",
            )
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

    # 3. Lock Invoice in consistent order (SEC-02)
    inv = session.execute(
        select(Invoice)
        .where(Invoice.id == pm.invoice_id, Invoice.tenant_id == tenant.id)
        .with_for_update()
    ).scalar_one()

    # Compute canonical reconciliation context
    ctx = compute_match_reconciliation_context(
        session,
        tenant_id=tenant.id,
        payment_match=pm,
        payment_line=pl,
        invoice=inv,
    )

    # Verify HMAC-signed stale-view token if provided (scope, 30m expiry, amounts parity)
    if approval_token:
        is_valid, err_msg = verify_match_approval_token(
            settings.secret_key,
            approval_token,
            expected_tenant_id=tenant.id,
            expected_user_id=user.id,
            expected_match_id=pm.id,
            current_matched_santim=ctx.matched_santim,
            current_invoice_amount_santim=ctx.invoice_total_santim,
            current_payment_amount_santim=ctx.payment_amount_santim,
            current_match_state=pm.state,
        )
        if not is_valid:
            return format_match_error_response(
                request,
                "stale_token_error",
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Stale view or invalid token: {err_msg}",
                match_id=pm.id,
            )

    prior_pl_allocated = ctx.prior_pl_allocated_santim
    total_confirmed_allocations = prior_pl_allocated + ctx.matched_santim
    if total_confirmed_allocations > ctx.payment_amount_santim:
        return format_match_error_response(
            request,
            "match_already_confirmed",
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Payment line is already confirmed or fully allocated ({prior_pl_allocated}/{ctx.payment_amount_santim} santim).",
            match_id=pm.id,
        )

    if ctx.invoice_open_santim <= 0:
        return format_match_error_response(
            request,
            "match_already_confirmed",
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Invoice {inv.invoice_number} is already fully settled.",
            match_id=pm.id,
        )

    open_balance = ctx.invoice_open_santim
    is_differing = ctx.is_differing
    if is_differing and not confirm_differing:
        return format_match_error_response(
            request,
            "amounts_differ_confirmation_required",
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Amounts differ (Match: {format_money(ctx.matched_santim)}, Invoice Open: {format_money(ctx.invoice_open_santim)}, Payment: {format_money(ctx.payment_amount_santim)}). Explicit confirmation required.",
            match_id=pm.id,
        )

    now = datetime.now(timezone.utc)
    pm.state = MatchState.CONFIRMED.value
    pm.confirmed_by = user.id
    pm.confirmed_at = now

    if total_confirmed_allocations >= pl.amount_santim:
        pl.state = PaymentState.CONFIRMED.value
    else:
        pl.state = PaymentState.PROPOSED.value

    # Update invoice state
    prior_inv_paid = ctx.invoice_total_santim - ctx.invoice_open_santim
    new_inv_paid = prior_inv_paid + pm.matched_santim
    if new_inv_paid >= inv.amount_santim:
        inv.state = InvoiceState.SETTLED.value
    else:
        inv.state = InvoiceState.PARTIALLY_PAID.value

    # Track processed token in idempotent table
    if approval_token:
        session.add(
            ProcessedApprovalToken(
                tenant_id=tenant.id,
                token_hash=hash_approval_token(approval_token),
                actor_id=user.id,
                payment_match_id=pm.id,
                result_status="confirmed",
            )
        )

    # Append immutable event
    append_event(
        session,
        tenant_id=tenant.id,
        event_type="match.web_confirmed",
        aggregate_type="payment_match",
        aggregate_id=pm.id,
        actor_id=user.id,
        payload={
            "invoice_id": str(inv.id),
            "matched_santim": pm.matched_santim,
            "is_differing": is_differing,
        },
    )

    session.commit()
    rem_count = get_pending_review_queue_count(session, tenant.id)

    return HTMLResponse(
        "<div data-testid='match-confirmed-banner' class='p-3 text-xs font-bold text-emerald-800 bg-emerald-50 rounded-lg'>✓ Match Confirmed</div>"
        f"<span id='review-queue-badge' hx-swap-oob='true' class='badge badge-warning'>{rem_count} Pending Items</span>"
        f"<div id='kpi-review-queue' hx-swap-oob='true' class='text-2xl font-black text-amber-600 mt-1'>{rem_count}</div>"
        f"<div id='step3-review-count' hx-swap-oob='true' class='text-3xl font-black text-amber-600'>{rem_count}</div>"
    )


@router.post("/matches/{match_id}/reverse")
def reverse_match(
    match_id: str,
    reason_code: str = Form(...),
    notes: str | None = Form(None),
    session: Session = Depends(get_session),
    owner_context: tuple[Tenant, User] = Depends(require_owner),
    _csrf: None = Depends(verify_csrf),
):
    """
    Owner reversal of a confirmed match.
    Enforces required reason code, logs compensating event (never UPDATE or DELETE on events),
    stores free-text in separate notes table, and restores open balances.
    """
    tenant, user = owner_context
    set_tenant(session, tenant.id)

    try:
        mid = uuid.UUID(match_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid match ID")

    # Validate reason code
    valid_codes = [c.value for c in ReversalReasonCode]
    if reason_code not in valid_codes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid reversal reason code '{reason_code}'. Must be one of: {valid_codes}",
        )

    # 1. Lock PaymentMatch in consistent order
    pm = session.execute(
        select(PaymentMatch)
        .where(PaymentMatch.id == mid, PaymentMatch.tenant_id == tenant.id)
        .with_for_update()
    ).scalar_one_or_none()

    if not pm:
        raise HTTPException(status_code=404, detail="Match not found")

    if pm.state == MatchState.REVERSED.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Match has already been reversed. Cannot reverse twice.",
        )

    if pm.state != MatchState.CONFIRMED.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only confirmed matches can be reversed.",
        )

    # 2. Lock PaymentLine in consistent order
    pl = session.execute(
        select(PaymentLine)
        .where(PaymentLine.id == pm.payment_line_id, PaymentLine.tenant_id == tenant.id)
        .with_for_update()
    ).scalar_one()

    # 3. Lock Invoice in consistent order
    inv = session.execute(
        select(Invoice)
        .where(Invoice.id == pm.invoice_id, Invoice.tenant_id == tenant.id)
        .with_for_update()
    ).scalar_one()

    # Transition match to reversed
    pm.state = MatchState.REVERSED.value

    # Update payment line: flag as previously reversed and return to review queue
    pl.is_previously_reversed = True

    # Recheck remaining confirmed allocations for payment line
    rem_pl_confirmed = session.execute(
        select(func.coalesce(func.sum(PaymentMatch.matched_santim), 0))
        .where(
            PaymentMatch.payment_line_id == pl.id,
            PaymentMatch.state == MatchState.CONFIRMED.value,
        )
    ).scalar_one()

    if rem_pl_confirmed >= pl.amount_santim:
        pl.state = PaymentState.CONFIRMED.value
    else:
        pl.state = PaymentState.PROPOSED.value

    # Recheck remaining confirmed payments for invoice
    rem_inv_confirmed = session.execute(
        select(func.coalesce(func.sum(PaymentMatch.matched_santim), 0))
        .where(
            PaymentMatch.invoice_id == inv.id,
            PaymentMatch.state == MatchState.CONFIRMED.value,
        )
    ).scalar_one()

    if rem_inv_confirmed == 0:
        inv.state = InvoiceState.OPEN.value
    elif rem_inv_confirmed < inv.amount_santim:
        inv.state = InvoiceState.PARTIALLY_PAID.value
    else:
        inv.state = InvoiceState.SETTLED.value

    # Append compensating event to immutable log (Never UPDATE/DELETE on events)
    append_event(
        session,
        tenant_id=tenant.id,
        event_type="match.reversed",
        aggregate_type="payment_match",
        aggregate_id=pm.id,
        actor_id=user.id,
        payload={
            "reason_code": reason_code,
            "reversed_santim": pm.matched_santim,
            "invoice_id": str(inv.id),
            "payment_line_id": str(pl.id),
            "relied_on_l3": (pl.evidence_level == "L3"),
        },
    )

    # Free-text notes stored in separate deletable notes table (no personal data in events)
    if notes and notes.strip():
        rev_note = ReversalNote(
            tenant_id=tenant.id,
            payment_match_id=pm.id,
            reason_code=reason_code,
            notes=notes.strip(),
            created_by=user.id,
        )
        session.add(rev_note)

    session.commit()
    return HTMLResponse(
        "<div class='p-3 text-xs font-bold text-amber-900 bg-amber-50 border border-amber-200 rounded-lg'>"
        "✓ Match Reversed. Balances and audit trail restored."
        "</div>"
    )


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
    append_event(
        session,
        tenant_id=tenant.id,
        event_type="payment_line.accepted_unverified",
        aggregate_type="payment_line",
        aggregate_id=pl.id,
        actor_id=user.id,
        payload={"evidence_level": pl.evidence_level, "amount_santim": pl.amount_santim},
    )

    # Transition any attached proposed match so no dangling proposed matches linger and log events
    attached_matches = session.execute(
        select(PaymentMatch)
        .where(
            PaymentMatch.payment_line_id == pl.id,
            PaymentMatch.tenant_id == tenant.id,
            PaymentMatch.state == MatchState.PROPOSED.value,
        )
    ).scalars().all()
    for pm_item in attached_matches:
        pm_item.state = MatchState.REJECTED.value
        append_event(
            session,
            tenant_id=tenant.id,
            event_type="match.rejected",
            aggregate_type="payment_match",
            aggregate_id=pm_item.id,
            actor_id=user.id,
            payload={"reason": "payment_line_accepted_unverified", "payment_line_id": str(pl.id)},
        )

    session.commit()
    rem_count = get_pending_review_queue_count(session, tenant.id)

    return HTMLResponse(
        "<div data-testid='match-unverified-banner' class='p-3 text-xs font-bold text-purple-900 bg-purple-50 rounded-lg flex items-center justify-between'>"
        "<span>✓ Marked Accepted Unverified (L4). Excluded from all paid reports.</span>"
        f"<form method='POST' action='/matches/{pid}/undo-unverified' style='margin: 0; display: inline-block;'>"
        f"<button type='submit' hx-post='/matches/{pid}/undo-unverified' hx-target='#match-card-{pid}' hx-swap='outerHTML' data-testid='undo-unverified-button' class='btn btn-secondary' style='padding: 0.2rem 0.6rem; font-size: 0.75rem; margin-left: 0.5rem;'>Undo</button>"
        "</form>"
        "</div>"
        f"<span id='review-queue-badge' hx-swap-oob='true' class='badge badge-warning'>{rem_count} Pending Items</span>"
        f"<div id='kpi-review-queue' hx-swap-oob='true' class='text-2xl font-black text-amber-600 mt-1'>{rem_count}</div>"
        f"<div id='step3-review-count' hx-swap-oob='true' class='text-3xl font-black text-amber-600'>{rem_count}</div>"
    )


@router.post("/matches/{match_id}/undo-unverified")
def undo_unverified(
    match_id: str,
    session: Session = Depends(get_session),
    owner_context: tuple[Tenant, User] = Depends(require_owner),
    _csrf: None = Depends(verify_csrf),
):
    """
    Undo an accepted_unverified marking: restores payment line to proposed
    and any attached rejected matches back to proposed.
    """
    tenant, user = owner_context
    set_tenant(session, tenant.id)
    try:
        pid = uuid.UUID(match_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid ID")

    pl = session.get(PaymentLine, pid)
    if not pl:
        pm = session.get(PaymentMatch, pid)
        if pm:
            pl = pm.payment_line

    if not pl or pl.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Record not found")

    if pl.state != PaymentState.ACCEPTED_UNVERIFIED.value:
        raise HTTPException(status_code=400, detail="Payment line is not in accepted_unverified state")

    pl.state = PaymentState.PROPOSED.value
    append_event(
        session,
        tenant_id=tenant.id,
        event_type="payment_line.unverified_acceptance_undone",
        aggregate_type="payment_line",
        aggregate_id=pl.id,
        actor_id=user.id,
        payload={"evidence_level": pl.evidence_level, "amount_santim": pl.amount_santim},
    )

    # Restore any attached matches that were rejected by mark_unverified
    attached_matches = session.execute(
        select(PaymentMatch)
        .where(
            PaymentMatch.payment_line_id == pl.id,
            PaymentMatch.tenant_id == tenant.id,
            PaymentMatch.state == MatchState.REJECTED.value,
        )
    ).scalars().all()
    for pm_item in attached_matches:
        pm_item.state = MatchState.PROPOSED.value
        append_event(
            session,
            tenant_id=tenant.id,
            event_type="match.restored_to_proposed",
            aggregate_type="payment_match",
            aggregate_id=pm_item.id,
            actor_id=user.id,
            payload={"reason": "unverified_acceptance_undone", "payment_line_id": str(pl.id)},
        )

    session.commit()
    rem_count = get_pending_review_queue_count(session, tenant.id)

    return HTMLResponse(
        "<div data-testid='match-undo-unverified-banner' class='p-3 text-xs font-bold text-slate-800 bg-slate-100 rounded-lg'>"
        "✓ Acceptance undone. Restored to pending review."
        "</div>"
        f"<span id='review-queue-badge' hx-swap-oob='true' class='badge badge-warning'>{rem_count} Pending Items</span>"
        f"<div id='kpi-review-queue' hx-swap-oob='true' class='text-2xl font-black text-amber-600 mt-1'>{rem_count}</div>"
        f"<div id='step3-review-count' hx-swap-oob='true' class='text-3xl font-black text-amber-600'>{rem_count}</div>"
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
    rem_count = get_pending_review_queue_count(session, tenant.id)

    return HTMLResponse(
        "<div data-testid='match-rejected-banner' class='p-3 text-xs font-bold text-slate-500 bg-slate-100 rounded-lg'>✕ Match Rejected</div>"
        f"<span id='review-queue-badge' hx-swap-oob='true' class='badge badge-warning'>{rem_count} Pending Items</span>"
        f"<div id='kpi-review-queue' hx-swap-oob='true' class='text-2xl font-black text-amber-600 mt-1'>{rem_count}</div>"
        f"<div id='step3-review-count' hx-swap-oob='true' class='text-3xl font-black text-amber-600'>{rem_count}</div>"
    )


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

@router.get("/exports/reversals.csv")
def download_reversals_csv(
    session: Session = Depends(get_session),
    owner_context: tuple[Tenant, User] = Depends(require_owner),
):
    """Owner-only export of all reversed payment matches with audit metadata."""
    tenant, _ = owner_context
    set_tenant(session, tenant.id)
    content = export_reversals_csv(session, tenant_id=tenant.id)
    return Response(
        content=content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=reversals.csv"},
    )


@router.post("/reversals/notes/{note_id}/delete")
def delete_reversal_note_endpoint(
    note_id: str,
    session: Session = Depends(get_session),
    owner_context: tuple[Tenant, User] = Depends(require_owner),
    _csrf: None = Depends(verify_csrf),
):
    """
    Owner-only deletion of a free-text reversal note.
    Logs immutable event recording only note_id and match_id, without storing note content.
    """
    tenant, user = owner_context
    set_tenant(session, tenant.id)
    try:
        nid = uuid.UUID(note_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid note UUID format.")

    note = session.get(ReversalNote, nid)
    if not note or note.tenant_id != tenant.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reversal note not found.")

    match_id = note.payment_match_id
    session.delete(note)
    session.flush()

    append_event(
        session=session,
        tenant_id=tenant.id,
        aggregate_type="reversal_note",
        aggregate_id=nid,
        event_type="reversal_note.deleted",
        payload={
            "reversal_note_id": str(nid),
            "payment_match_id": str(match_id),
            "deleted_by": str(user.id),
        },
    )
    session.commit()
    return HTMLResponse("<div class='text-xs font-semibold text-slate-500'>Note deleted</div>")



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

    lang = request.cookies.get("lang", "en")
    return templates.TemplateResponse(
        request=request,
        name="confirm_balance.html",
        context={
            "tenant": tenant,
            "retailer": retailer,
            "invoices": invoices,
            "total_outstanding_etb": to_etb(total_santim),
            "as_of_date": today,
            "expires_at": token_rec.expires_at,
            "raw_token": raw_token,
            "lang": lang,
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
