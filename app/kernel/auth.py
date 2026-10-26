"""
app/kernel/auth.py

Core authentication domain logic:
  - Argon2id password hashing and verification
  - Opaque random session tokens (stored hashed with SHA-256 in PostgreSQL)
  - Idle timeout & absolute session expiration
  - CSRF synchronizer token generation and validation
  - Session revocation and "logout everywhere"

Invariants:
  - Never import FastAPI or aiogram here.
  - Zero plaintext passwords in memory or database.
  - Passwords hashed exclusively with Argon2id (min 10 chars).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Sequence

import argon2
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.kernel.events import append_event
from app.kernel.models import Session as SessionModel, Tenant, User
from app.kernel.tenant import set_tenant
from app.matching.normalization import normalize_phone


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class AuthError(Exception):
    """Base authentication error."""


class InvalidCredentialsError(AuthError):
    """Invalid identifier or password."""


class AccountLockedError(AuthError):
    """Account is temporarily locked due to repeated failed logins."""


class UserDeactivatedError(AuthError):
    """User account is deactivated."""


class SessionError(AuthError):
    """Session validation error."""


class SessionNotFoundError(SessionError):
    """Session not found."""


class SessionExpiredError(SessionError):
    """Session exceeded absolute lifetime."""


class SessionIdleTimeoutError(SessionError):
    """Session exceeded idle inactivity timeout."""


class CsrfError(AuthError):
    """CSRF token invalid or missing."""


# ---------------------------------------------------------------------------
# Argon2id Password Management
# ---------------------------------------------------------------------------
_hasher = argon2.PasswordHasher(
    time_cost=3,
    memory_cost=65536,  # 64 MB
    parallelism=4,
    type=argon2.Type.ID,
)


def hash_password(password: str) -> str:
    """Hash password using Argon2id with minimum length enforcement."""
    if len(password) < 10:
        raise ValueError("Password must be at least 10 characters long.")
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    """Verify password against Argon2id hash."""
    if not password_hash:
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, Exception):
        return False


def normalize_identifier(raw: str) -> str:
    """Normalize email (lowercased) or Ethiopian phone number (+251...)."""
    clean = raw.strip()
    if "@" in clean:
        return clean.lower()
    phone_norm = normalize_phone(clean)
    return phone_norm if phone_norm else clean


# ---------------------------------------------------------------------------
# Session Token Management
# ---------------------------------------------------------------------------
def _hash_token(raw_token: str) -> str:
    """Compute SHA-256 hash of token."""
    return hashlib.sha256(raw_token.strip().encode("utf-8")).hexdigest()


def create_user_session(
    session: Session,
    *,
    user: User,
    ip_address: str | None = None,
    user_agent: str | None = None,
    lifetime_hours: int = 12,
) -> tuple[SessionModel, str]:
    """
    Generate an opaque 256-bit CSPRNG session token.
    Stores the SHA-256 hash in DB; returns (session_record, raw_token_string).
    """
    raw_token = secrets.token_urlsafe(32)
    token_hash = _hash_token(raw_token)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=lifetime_hours)

    set_tenant(session, user.tenant_id)

    session_rec = SessionModel(
        id=uuid.uuid4(),
        tenant_id=user.tenant_id,
        user_id=user.id,
        token_hash=token_hash,
        created_at=now,
        last_activity_at=now,
        expires_at=expires_at,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    session.add(session_rec)
    session.flush()

    return session_rec, raw_token


def validate_session_token(
    session: Session,
    *,
    raw_token: str,
    idle_timeout_minutes: int = 60,
) -> tuple[SessionModel, User, Tenant]:
    """
    Validate a raw session token string against the database.
    Enforces existence, user active status, absolute lifetime, and idle timeout.
    Returns (session_record, user, tenant).
    """
    if not raw_token or not raw_token.strip():
        raise SessionNotFoundError("Missing session token.")

    thash = _hash_token(raw_token)
    session_rec = None

    if session.bind and session.bind.dialect.name == "postgresql":
        from sqlalchemy import text
        row = session.execute(
            text("SELECT id, tenant_id, user_id, expires_at, last_activity_at FROM lookup_session_by_token_hash(:thash)"),
            {"thash": thash},
        ).mappings().first()
        if not row:
            raise SessionNotFoundError("Session not found or invalid.")
        set_tenant(session, row["tenant_id"])
        session_rec = session.get(SessionModel, row["id"])
    else:
        session_rec = session.execute(
            select(SessionModel).where(SessionModel.token_hash == thash)
        ).scalar_one_or_none()

    if not session_rec or not hmac.compare_digest(session_rec.token_hash, thash):
        raise SessionNotFoundError("Session not found or invalid.")

    now = datetime.now(timezone.utc)

    # 1. Absolute lifetime check
    if session_rec.expires_at < now:
        session.delete(session_rec)
        session.flush()
        raise SessionExpiredError("Session has expired. Please log in again.")

    # 2. Idle timeout check
    idle_cutoff = session_rec.last_activity_at + timedelta(minutes=idle_timeout_minutes)
    if idle_cutoff < now:
        session.delete(session_rec)
        session.flush()
        raise SessionIdleTimeoutError("Session timed out due to inactivity. Please log in again.")

    # 3. User & active check
    user = session.get(User, session_rec.user_id)
    if not user or not user.is_active:
        session.delete(session_rec)
        session.flush()
        raise UserDeactivatedError("User account is inactive or not found.")

    tenant = session.get(Tenant, session_rec.tenant_id)
    if not tenant:
        raise SessionNotFoundError("Tenant account associated with session not found.")

    # Update last activity timestamp
    session_rec.last_activity_at = now
    session.flush()

    # Establish RLS tenant context for downstream queries
    set_tenant(session, user.tenant_id)

    return session_rec, user, tenant


def terminate_session(session: Session, *, raw_token: str) -> bool:
    """Terminate a single session by raw token string."""
    if not raw_token:
        return False
    thash = _hash_token(raw_token)
    res = session.execute(
        delete(SessionModel).where(SessionModel.token_hash == thash)
    )
    session.flush()
    return res.rowcount > 0


def terminate_all_user_sessions(session: Session, *, user_id: uuid.UUID) -> int:
    """Revoke all sessions for a specific user (log out everywhere)."""
    res = session.execute(
        delete(SessionModel).where(SessionModel.user_id == user_id)
    )
    session.flush()
    return res.rowcount


# ---------------------------------------------------------------------------
# CSRF Synchronizer Token Management
# ---------------------------------------------------------------------------
def generate_csrf_token(session_id: uuid.UUID, secret_key: str) -> str:
    """Generate a cryptographic CSRF synchronizer token bound to this session ID."""
    msg = session_id.bytes + b":csrf"
    return hmac.new(secret_key.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def validate_csrf_token(session_id: uuid.UUID, csrf_token: str | None, secret_key: str) -> bool:
    """Constant-time validation of CSRF token against session ID."""
    if not csrf_token or not csrf_token.strip():
        return False
    expected = generate_csrf_token(session_id, secret_key)
    return hmac.compare_digest(expected, csrf_token.strip())
