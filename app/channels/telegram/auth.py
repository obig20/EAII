"""
app/channels/telegram/auth.py

Telegram user authentication, persistent invite-code registration in PostgreSQL,
and session resolution.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.kernel.enums import UserRole
from app.kernel.events import append_event
from app.kernel.models import TelegramInvite, Tenant, User
from app.kernel.tenant import set_tenant


# Thread-safe in-memory store for UI language preferences (en/am)
_USER_LANGUAGES: dict[int, str] = {}


def generate_invite_code(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    role: str = "cashier",
    display_name: str = "Telegram User",
    expires_in_hours: int = 72,
) -> str:
    """Generate an alphanumeric invite code stored hashed in PostgreSQL."""
    code = f"DC-{secrets.token_hex(3).upper()}"
    code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc)

    invite = TelegramInvite(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        code_hash=code_hash,
        role=role,
        display_name=display_name,
        expires_at=now + timedelta(hours=expires_in_hours),
        used_at=None,
        used_by_telegram_id=None,
    )
    session.add(invite)
    session.flush()

    append_event(
        session,
        tenant_id=tenant_id,
        event_type="auth.telegram_invite_created",
        aggregate_type="telegram_invite",
        aggregate_id=invite.id,
        payload={"role": role},
    )

    return code


def redeem_invite_code(
    session: Session,
    *,
    code: str,
    telegram_id: int,
) -> tuple[Tenant, User] | None:
    """
    Validate invite code against PostgreSQL and link Telegram ID to tenant user.
    Single-use only.
    """
    clean_code = code.strip().upper()
    code_hash = hashlib.sha256(clean_code.encode("utf-8")).hexdigest()

    invite = None
    if session.bind and session.bind.dialect.name == "postgresql":
        from sqlalchemy import text
        row = session.execute(
            text("SELECT id, tenant_id, role, display_name, expires_at, used_at FROM lookup_telegram_invite(:chash)"),
            {"chash": code_hash},
        ).mappings().first()
        if not row:
            return None
        set_tenant(session, row["tenant_id"])
        invite = session.get(TelegramInvite, row["id"])
    else:
        invite = session.execute(
            select(TelegramInvite).where(TelegramInvite.code_hash == code_hash)
        ).scalar_one_or_none()

    if not invite:
        return None

    now = datetime.now(timezone.utc)
    if invite.expires_at < now or invite.used_at is not None:
        return None

    tenant = session.get(Tenant, invite.tenant_id)
    if not tenant:
        return None

    set_tenant(session, tenant.id)

    # Check if a user for this telegram_id already exists in this tenant
    user = session.execute(
        select(User).where(User.tenant_id == tenant.id, User.telegram_id == telegram_id)
    ).scalar_one_or_none()

    if not user:
        user = User(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            role=invite.role,
            display_name=invite.display_name,
            telegram_id=telegram_id,
            is_active=True,
            created_at=now,
        )
        session.add(user)
    else:
        user.role = invite.role
        user.is_active = True

    invite.used_at = now
    invite.used_by_telegram_id = telegram_id
    session.flush()

    append_event(
        session,
        tenant_id=tenant.id,
        event_type="user.telegram_registered",
        aggregate_type="user",
        aggregate_id=user.id,
        actor_id=user.id,
        payload={
            "role": user.role,
        },
    )

    return tenant, user


def get_user_by_telegram_id(
    session: Session,
    telegram_id: int,
) -> tuple[Tenant, User] | None:
    """Look up tenant and user record by registered Telegram ID."""
    user = None
    if session.bind and session.bind.dialect.name == "postgresql":
        from sqlalchemy import text
        row = session.execute(
            text("SELECT id, tenant_id, role, display_name, is_active FROM lookup_user_by_telegram_id(:tg_id)"),
            {"tg_id": telegram_id},
        ).mappings().first()
        if not row or not row["is_active"]:
            return None
        set_tenant(session, row["tenant_id"])
        user = session.get(User, row["id"])
    else:
        user = session.execute(
            select(User).where(User.telegram_id == telegram_id)
        ).scalars().first()

    if not user or not user.is_active:
        return None

    set_tenant(session, user.tenant_id)
    tenant = session.get(Tenant, user.tenant_id)
    if not tenant:
        return None

    return tenant, user


def get_user_language(telegram_id: int) -> str:
    """Return 'en' or 'am'."""
    return _USER_LANGUAGES.get(telegram_id, "en")


def set_user_language(telegram_id: int, lang: str) -> None:
    """Set language preference."""
    if lang.lower() in ("am", "amharic"):
        _USER_LANGUAGES[telegram_id] = "am"
    else:
        _USER_LANGUAGES[telegram_id] = "en"
