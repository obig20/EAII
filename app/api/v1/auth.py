"""
app/api/v1/auth.py

JSON auth endpoints for the Daily Close PWA:
  POST /api/v1/auth/login   – exchange credentials for a Bearer token
  POST /api/v1/auth/logout  – terminate the current session
  GET  /api/v1/auth/me      – return the authenticated principal

Rules:
  Rule 3 – set_tenant() is called immediately after session validation
            (done inside deps._authenticate, which api_auth calls).
            Login is the exception: tenant is unknown until we find the user,
            so we call set_tenant() ourselves after the lookup.
  Rule 4 – every state change appends an event.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.api.v1.deps import ApiPrincipal, _bearer_token, api_auth
from app.api.v1.schemas import AuthOut, LoginIn, OkOut, UserOut
from app.channels.web.routes import _get_client_ip
from app.kernel.auth import (
    AuthError,
    create_user_session,
    normalize_identifier,
    terminate_session,
    verify_password,
)
from app.kernel.db import get_session
from app.kernel.events import append_event
from app.kernel.models import User
from app.kernel.persistent_rate_limit import (
    check_persistent_rate_limit,
    record_failed_login,
    record_successful_login,
)
from app.kernel.tenant import set_tenant

router = APIRouter(prefix="/auth", tags=["auth"])

_LOGIN_MAX_REQUESTS = 10
_LOGIN_WINDOW_SECONDS = 60


def _user_out(user: User, tenant_name: str) -> UserOut:
    return UserOut(
        id=str(user.id),
        displayName=str(user.display_name or user.email or user.phone or ""),
        role=str(user.role),
        tenantId=str(user.tenant_id),
        tenantName=tenant_name,
    )


@router.post("/login", response_model=AuthOut)
async def login(
    body: LoginIn,
    request: Request,
    session: Session = Depends(get_session),
) -> AuthOut:
    client_ip: str = _get_client_ip(request) or "unknown"
    user_agent: str | None = request.headers.get("User-Agent")

    # Rate-limit by IP to slow brute-force attempts.
    try:
        check_persistent_rate_limit(
            session,
            key=f"login:ip:{client_ip}",
            max_requests=_LOGIN_MAX_REQUESTS,
            window_seconds=_LOGIN_WINDOW_SECONDS,
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts. Please wait a minute and try again.",
        )

    normalized = normalize_identifier(body.identifier)

    # Look up the user globally (tenant is unknown at this point).
    user: User | None = session.execute(
        select(User).where(
            User.is_active.is_(True),
            or_(
                func.lower(User.email) == normalized,
                User.phone == normalized,
                func.lower(User.display_name) == normalized,
            ),
        )
    ).scalar_one_or_none()

    if user is None or not verify_password(user.password_hash, body.password):
        record_failed_login(
            session,
            user=user,
            ip_address=client_ip,
            tenant_id=user.tenant_id if user else None,
            identifier=body.identifier,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Check account lock.
    from datetime import datetime, timezone
    if user.locked_until is not None:
        now = datetime.now(timezone.utc)
        lu = user.locked_until
        if lu.tzinfo is None:
            from datetime import timezone as _tz
            lu = lu.replace(tzinfo=_tz.utc)
        if lu > now:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Account is temporarily locked. Try again later.",
            )

    # Rule 3 – bind tenant context before any further queries.
    set_tenant(session, user.tenant_id)

    record_successful_login(session, user=user, ip_address=client_ip)

    _session_rec, raw_token = create_user_session(
        session,
        user=user,
        ip_address=client_ip,
        user_agent=user_agent,
    )

    # Rule 4 – audit event.
    append_event(
        session,
        tenant_id=user.tenant_id,
        event_type="auth.api_login",
        aggregate_type="user",
        aggregate_id=user.id,
        actor_id=user.id,
        payload={"ip": client_ip},
    )

    tenant_name = str(getattr(user, "tenant", None) and user.tenant.name or "")
    # Eager-load tenant name if the relationship is available.
    try:
        from app.kernel.models import Tenant
        tenant_obj = session.get(Tenant, user.tenant_id)
        tenant_name = str(tenant_obj.name) if tenant_obj else ""
    except Exception:
        tenant_name = ""

    return AuthOut(
        ok=True,
        token=raw_token,
        user=_user_out(user, tenant_name),
    )


@router.post("/logout", response_model=OkOut)
async def logout(
    request: Request,
    principal: ApiPrincipal = Depends(api_auth),
    session: Session = Depends(get_session),
) -> OkOut:
    # Extract the raw token that was used to authenticate this request.
    raw_token = _bearer_token(request) or request.cookies.get("dc_session", "")

    if raw_token:
        terminate_session(session, raw_token=raw_token)

    # Rule 4 – audit event.
    append_event(
        session,
        tenant_id=principal.tenant.id,
        event_type="auth.api_logout",
        aggregate_type="user",
        aggregate_id=principal.user.id,
        actor_id=principal.user.id,
        payload={},
    )

    return OkOut(ok=True)


@router.get("/me", response_model=AuthOut)
async def me(
    principal: ApiPrincipal = Depends(api_auth),
) -> AuthOut:
    tenant_name = str(getattr(principal.tenant, "name", ""))
    return AuthOut(
        ok=True,
        token="",  # We don't re-issue the token on /me
        user=_user_out(principal.user, tenant_name),
    )
