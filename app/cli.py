"""
app/cli.py

Command-line administrative utilities for Daily Close:
- Bootstrap initial tenant and owner account (no self-service signups)
- Generate persistent Telegram staff registration invites
"""

from __future__ import annotations

import argparse
import getpass
import sys
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.channels.telegram.auth import generate_invite_code
from app.kernel.auth import hash_password, normalize_identifier
from app.kernel.db import session_scope
from app.kernel.enums import UserRole
from app.kernel.events import append_event
from app.kernel.models import Tenant, User
from app.kernel.tenant import set_tenant
from app.matching.normalization import normalize_phone


def bootstrap_owner(args: argparse.Namespace) -> int:
    """Bootstrap a tenant and initial owner user."""
    identifier = args.identifier.strip()
    tenant_name = args.tenant.strip()
    display_name = args.name.strip()
    password = args.password

    if not password:
        password = getpass.getpass("Enter owner password (min 10 characters): ")
        confirm = getpass.getpass("Confirm owner password: ")
        if password != confirm:
            print("Error: Passwords do not match.", file=sys.stderr)
            return 1

    if len(password) < 10:
        print("Error: Password must be at least 10 characters long.", file=sys.stderr)
        return 1

    clean_id = normalize_identifier(identifier)
    email = clean_id if "@" in clean_id else None
    phone = clean_id if "@" not in clean_id else None

    with session_scope() as session:
        # Check if tenant exists or create new
        tenant = session.execute(
            select(Tenant).where(Tenant.name == tenant_name)
        ).scalars().first()

        now = datetime.now(timezone.utc)
        if not tenant:
            tenant = Tenant(
                id=uuid.uuid4(),
                name=tenant_name,
                created_at=now,
            )
            session.add(tenant)
            session.flush()
            print(f"Created new tenant: {tenant.name} (id={tenant.id})")
        else:
            print(f"Using existing tenant: {tenant.name} (id={tenant.id})")

        set_tenant(session, tenant.id)

        # Check if user already exists
        existing_user = session.execute(
            select(User).where(
                (User.tenant_id == tenant.id) & (
                    ((User.email == email) & (email.isnot(None))) |
                    ((User.phone == phone) & (phone.isnot(None)))
                )
            )
        ).scalars().first()

        if existing_user:
            print(f"Error: User with identifier '{clean_id}' already exists in this tenant.", file=sys.stderr)
            return 1

        pwd_hash = hash_password(password)
        new_user = User(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            role=UserRole.OWNER.value,
            display_name=display_name,
            email=email,
            phone=phone,
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
            actor_id=new_user.id,
            payload={"role": "owner", "bootstrap": True},
        )

        print(f"Successfully bootstrapped Owner account:")
        print(f"  User ID:      {new_user.id}")
        print(f"  Display Name: {new_user.display_name}")
        print(f"  Identifier:   {clean_id}")
        print(f"  Tenant:       {tenant.name} ({tenant.id})")
        return 0


def create_invite(args: argparse.Namespace) -> int:
    """Generate a staff invite code for Telegram."""
    tenant_id_str = args.tenant_id
    role = args.role.lower()
    display_name = args.name.strip()
    days = args.days

    if role not in (UserRole.OWNER.value, UserRole.CASHIER.value):
        print("Error: Role must be 'owner' or 'cashier'.", file=sys.stderr)
        return 1

    try:
        tid = uuid.UUID(tenant_id_str)
    except ValueError:
        print("Error: Invalid tenant UUID format.", file=sys.stderr)
        return 1

    with session_scope() as session:
        tenant = session.get(Tenant, tid)
        if not tenant:
            print(f"Error: Tenant {tid} not found.", file=sys.stderr)
            return 1

        set_tenant(session, tenant.id)
        raw_code = generate_invite_code(
            session,
            tenant_id=tenant.id,
            role=role,
            display_name=display_name,
            expires_in_days=days,
        )

        print(f"Generated Telegram Staff Invite:")
        print(f"  Invite Code:  {raw_code}")
        print(f"  Role:         {role}")
        print(f"  Name:         {display_name}")
        print(f"  Tenant:       {tenant.name}")
        print(f"  Instructions: In Telegram bot, send: /register {raw_code}")
        return 0


def prune_auth_failures(args: argparse.Namespace) -> int:
    """Prune auth_failures older than specified days (default: 7)."""
    days = args.days
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import text

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    with session_scope() as session:
        result = session.execute(
            text("DELETE FROM auth_failures WHERE failed_at < :cutoff"),
            {"cutoff": cutoff},
        )
        count = result.rowcount
        print(f"Pruned {count} auth failure record(s) older than {days} days.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Daily Close Admin CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # bootstrap-owner
    p_boot = subparsers.add_parser("bootstrap-owner", help="Bootstrap initial tenant and owner account")
    p_boot.add_argument("--tenant", required=True, help="Tenant / Business name")
    p_boot.add_argument("--identifier", required=True, help="Owner email or phone (+251...)")
    p_boot.add_argument("--name", required=True, help="Owner display name")
    p_boot.add_argument("--password", required=False, default=None, help="Owner password (min 10 chars; prompts if omitted)")
    p_boot.set_defaults(func=bootstrap_owner)

    # create-telegram-invite
    p_inv = subparsers.add_parser("create-telegram-invite", help="Generate Telegram registration invite code")
    p_inv.add_argument("--tenant-id", required=True, help="Tenant UUID")
    p_inv.add_argument("--role", required=True, choices=["owner", "cashier"], help="User role")
    p_inv.add_argument("--name", required=True, help="Staff display name")
    p_inv.add_argument("--days", type=int, default=7, help="Expiry in days (default: 7)")
    p_inv.set_defaults(func=create_invite)

    # prune-auth-failures
    p_prune = subparsers.add_parser("prune-auth-failures", help="Prune auth_failures older than N days")
    p_prune.add_argument("--days", type=int, default=7, help="Prune failures older than N days (default: 7)")
    p_prune.set_defaults(func=prune_auth_failures)


    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
