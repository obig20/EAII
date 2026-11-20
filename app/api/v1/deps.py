"""
app/api/v1/deps.py

Authentication dependencies for /api/v1.

Credentials: ``Authorization: Bearer <session token>`` (preferred for the PWA),
or the existing ``dc_session`` cookie.  Cookie-authenticated requests that
change state must also carry a valid ``X-CSRF-Token`` (same synchroniser token
as the web channel); Bearer requests are not ambient credentials so they need
no CSRF token.

Rule 3: every dependency binds the PostgreSQL RLS tenant context immediately
after the session is validated.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.channels.web.auth_deps import SESSION_COOKIE_NAME
from app.kernel.auth import (
    AuthError,
    validate_csrf_token,
    validate_session_token,
)
from app.kernel.db import get_session
from app.kernel.enums import UserRole
from app.kernel.models import Tenant, User
from app.kernel.tenant import set_tenant
from app.settings import settings

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True)
class ApiPrincipal:
    tenant: Tenant
    user: User
    session_id: object
    via_cookie: bool


def no_store(response: Response) -> None:
    """Financial data must not be cached by browsers or intermediaries."""
    response.headers["Cache-Control"] = "no-store"


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
        return token or None
    return None


def _authenticate(request: Request, session: Session) -> ApiPrincipal:
    bearer = _bearer_token(request)
    raw_token = bearer or request.cookies.get(SESSION_COOKIE_NAME)
    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        session_rec, user, tenant = validate_session_token(
            session,
            raw_token=raw_token,
            idle_timeout_minutes=settings.session_idle_timeout_minutes,
        )
    except AuthError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Rule 3: tenant context immediately, before any protected query.
    set_tenant(session, tenant.id)

    via_cookie = bearer is None
    if via_cookie and request.method not in _SAFE_METHODS:
        csrf = request.headers.get("x-csrf-token")
        if not validate_csrf_token(session_rec.id, csrf, settings.secret_key):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid or missing CSRF token.",
            )

    return ApiPrincipal(tenant=tenant, user=user, session_id=session_rec.id, via_cookie=via_cookie)


def api_auth(
    request: Request,
    session: Session = Depends(get_session),
) -> ApiPrincipal:
    """Require an authenticated user (owner or cashier)."""
    return _authenticate(request, session)


def api_owner(principal: ApiPrincipal = Depends(api_auth)) -> ApiPrincipal:
    """Require an authenticated user with the owner role."""
    if principal.user.role != UserRole.OWNER.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Owner role required for this action.",
        )
    return principal


def try_authenticate(request: Request, session: Session) -> ApiPrincipal | None:
    """Best-effort authentication for public routes with an authenticated variant. Never raises."""
    try:
        return _authenticate(request, session)
    except HTTPException:
        return None
