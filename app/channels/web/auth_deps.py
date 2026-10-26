"""
app/channels/web/auth_deps.py

Web channel authentication dependencies:
  - Session cookie resolution and verification
  - CSRF synchronizer token verification
  - Role-based authorization guards (Owner vs Cashier)
  - Automatic PostgreSQL RLS tenant context binding
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.kernel.auth import (
    SessionError,
    UserDeactivatedError,
    generate_csrf_token,
    validate_csrf_token,
    validate_session_token,
)
from app.kernel.db import get_session
from app.kernel.enums import UserRole
from app.kernel.models import Session as SessionModel, Tenant, User
from app.kernel.tenant import set_tenant
from app.settings import settings

SESSION_COOKIE_NAME = "dc_session"


def get_current_session_info(
    request: Request,
    session: Session = Depends(get_session),
) -> tuple[Tenant, User, SessionModel]:
    """
    Resolve and validate session from HttpOnly cookie (or Bearer header for tests).
    Raises HTTP 401 if missing, expired, idle timed out, or user deactivated.
    Establishes PostgreSQL RLS tenant context immediately.
    """
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)

    # Also check Authorization header (e.g. Bearer <token>) for API clients
    if not raw_token:
        auth_hdr = request.headers.get("Authorization")
        if auth_hdr and auth_hdr.startswith("Bearer "):
            raw_token = auth_hdr[7:].strip()

    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please log in.",
        )

    try:
        session_rec, user, tenant = validate_session_token(
            session,
            raw_token=raw_token,
            idle_timeout_minutes=settings.session_idle_timeout_minutes,
        )
    except (SessionError, UserDeactivatedError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        )

    # RLS tenant context is bound by validate_session_token
    return tenant, user, session_rec


def get_current_user_and_tenant(
    session_info: tuple[Tenant, User, SessionModel] = Depends(get_current_session_info),
) -> tuple[Tenant, User]:
    """Dependency returning authenticated (Tenant, User)."""
    return session_info[0], session_info[1]


def require_owner(
    session_info: tuple[Tenant, User, SessionModel] = Depends(get_current_session_info),
) -> tuple[Tenant, User]:
    """Dependency enforcing that the authenticated user has the 'owner' role."""
    tenant, user, _ = session_info
    if user.role != UserRole.OWNER.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Owner role required for this action.",
        )
    return tenant, user


async def verify_csrf(
    request: Request,
    session_info: tuple[Tenant, User, SessionModel] = Depends(get_current_session_info),
) -> None:
    """
    Enforce CSRF synchronizer token on state-changing HTTP methods.
    Checks X-CSRF-Token header or form field 'csrf_token'.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return

    # Extract CSRF token from header or form body
    csrf_token = request.headers.get("x-csrf-token")
    if not csrf_token:
        # Check form data if content-type is form
        content_type = request.headers.get("content-type", "")
        if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
            try:
                form_data = await request.form()
                csrf_val = form_data.get("csrf_token")
                if isinstance(csrf_val, str):
                    csrf_token = csrf_val
            except Exception:
                pass

    session_rec = session_info[2]
    if not csrf_token or not validate_csrf_token(session_rec.id, csrf_token, settings.secret_key):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Invalid or missing CSRF token.",
        )
