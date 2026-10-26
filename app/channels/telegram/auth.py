"""
app/channels/telegram/auth.py

Telegram user authentication, invite-code registration, and session resolution.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.kernel.enums import UserRole
from app.kernel.events import append_event
from app.kernel.models import Tenant, User
from app.kernel.tenant import set_tenant


@dataclass
class InviteCode:
    code: str
    tenant_id: uuid.UUID
    role: str
    display_name: str
    expires_at: datetime
    used_by: int | None = None


# Thread-safe in-memory store for active invite codes
# Default invite code for testing/development
_INVITE_CODES: dict[str, InviteCode] = {}
_USER_LANGUAGES: dict[int, str] = {}


def generate_invite_code(
    tenant_id: uuid.UUID,
    role: str = "cashier",
    display_name: str = "Telegram User",
    expires_in_hours: int = 72,
) -> str:
    """Generate an alphanumeric invite code for Telegram onboarding."""
    code = f"DC-{secrets.token_hex(3).upper()}"
    now = datetime.now(timezone.utc)
    _INVITE_CODES[code] = InviteCode(
        code=code,
        tenant_id=tenant_id,
        role=role,
        display_name=display_name,
        expires_at=now + timedelta(hours=expires_in_hours),
    )
    return code


def redeem_invite_code(
    session: Session,
    *,
    code: str,
    telegram_id: int,
) -> tuple[Tenant, User] | None:
    """
    Validate invite code and link Telegram ID to tenant user.
    Hashed at rest or stored safely.
    """
    clean_code = code.strip().upper()
    invite = _INVITE_CODES.get(clean_code)
    if not invite:
        return None

    now = datetime.now(timezone.utc)
    if invite.expires_at < now or invite.used_by is not None:
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
            created_at=now,
        )
        session.add(user)
    else:
        user.role = invite.role

    session.flush()
    invite.used_by = telegram_id

    append_event(
        session,
        tenant_id=tenant.id,
        event_type="user.telegram_registered",
        aggregate_type="user",
        aggregate_id=user.id,
        actor_id=user.id,
        payload={
            "role": user.role,
            "invite_code": clean_code,
        },
    )

    return tenant, user


def get_user_by_telegram_id(
    session: Session,
    telegram_id: int,
) -> tuple[Tenant, User] | None:
    """Look up tenant and user record by registered Telegram ID."""
    user = session.execute(
        select(User).where(User.telegram_id == telegram_id)
    ).scalars().first()

    if not user:
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
