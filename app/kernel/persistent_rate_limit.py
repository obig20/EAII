"""
app/kernel/persistent_rate_limit.py

Persistent rate limiting and account lockout stored in PostgreSQL.
Survives application restarts and works across multi-worker deployments.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.kernel.events import append_event
from app.kernel.models import RateLimitBucket, User


class RateLimitExceededError(Exception):
    """Raised when rate limit is exceeded or resource is locked out."""


def check_persistent_rate_limit(
    session: Session,
    *,
    key: str,
    max_requests: int,
    window_seconds: int = 60,
    lockout_seconds: int = 0,
) -> None:
    """
    Check and increment request count in PostgreSQL.
    Raises RateLimitExceededError if max_requests is exceeded within window.
    """
    now = datetime.now(timezone.utc)
    bucket = session.get(RateLimitBucket, key)

    if bucket:
        # Check active lockout
        if bucket.locked_until and bucket.locked_until > now:
            remaining = int((bucket.locked_until - now).total_seconds())
            raise RateLimitExceededError(
                f"Rate limit exceeded. Temporary lockout active for {remaining} seconds."
            )

        # Check if window expired
        window_cutoff = now - timedelta(seconds=window_seconds)
        if bucket.last_attempt_at < window_cutoff:
            bucket.attempts = 1
            bucket.first_attempt_at = now
            bucket.last_attempt_at = now
            bucket.locked_until = None
        else:
            if bucket.attempts >= max_requests:
                if lockout_seconds > 0:
                    bucket.locked_until = now + timedelta(seconds=lockout_seconds)
                session.flush()
                raise RateLimitExceededError("Too many requests. Please wait before trying again.")
            bucket.attempts += 1
            bucket.last_attempt_at = now
    else:
        bucket = RateLimitBucket(
            key=key,
            attempts=1,
            first_attempt_at=now,
            last_attempt_at=now,
            locked_until=None,
        )
        session.add(bucket)

    session.flush()


def record_failed_login(
    session: Session,
    *,
    user: User | None,
    ip_address: str,
    tenant_id: uuid.UUID | None = None,
    identifier: str | None = None,
) -> None:
    """
    Track consecutive failed login attempts on a user account and emit append-only audit event.
    Locks the user account for 15 minutes after 5 consecutive failures.
    When user is None (unknown identifier), records into auth_failures with HMAC-SHA256,
    never writing foreign-key-violating dummy tenant ids into the events table (SEC-12, SEC-06).
    """
    now = datetime.now(timezone.utc)

    if user:
        user.failed_login_attempts += 1
        if user.failed_login_attempts >= 5:
            user.locked_until = now + timedelta(minutes=15)
        session.flush()

        append_event(
            session,
            tenant_id=user.tenant_id,
            event_type="auth.login_failure",
            aggregate_type="user",
            aggregate_id=user.id,
            actor_id=user.id,
            payload={
                "ip_address": ip_address,
                "locked": bool(user.locked_until and user.locked_until > now),
            },
        )
    else:
        # Unknown identifier: store in dedicated auth_failures table without tenant FK
        import hashlib
        import hmac
        from app.kernel.models import AuthFailure
        from app.settings import settings

        raw_id = identifier or "unknown"
        ident_hmac = hmac.new(
            settings.auth_log_key.encode("utf-8"),
            raw_id.strip().lower().encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        failure = AuthFailure(
            identifier_hmac=ident_hmac,
            ip_address=ip_address,
            failed_at=now,
        )
        session.add(failure)
        session.flush()


def record_successful_login(
    session: Session,
    *,
    user: User,
    ip_address: str,
) -> None:
    """Reset failed attempts and record successful login event."""
    user.failed_login_attempts = 0
    user.locked_until = None
    session.flush()

    append_event(
        session,
        tenant_id=user.tenant_id,
        event_type="auth.login_success",
        aggregate_type="user",
        aggregate_id=user.id,
        actor_id=user.id,
        payload={"ip_address": ip_address},
    )
