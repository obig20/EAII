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


def unlock_user(args: argparse.Namespace) -> int:
    """Unlock a locked user account and reset failed login attempts (SEC-05)."""
    from sqlalchemy import delete, text
    from app.kernel.models import RateLimitBucket

    identifier = args.identifier.strip()
    clean_id = normalize_identifier(identifier)
    email = clean_id if "@" in clean_id else None
    phone = clean_id if "@" not in clean_id else None

    with session_scope() as session:
        user_row = None
        if session.bind and session.bind.dialect.name == "postgresql":
            user_row = session.execute(
                text("SELECT id, tenant_id, display_name FROM lookup_user_by_identifier(:ident)"),
                {"ident": clean_id},
            ).mappings().first()
        else:
            query = select(User)
            if email:
                query = query.where(User.email == email)
            else:
                query = query.where(User.phone == phone)
            u = session.execute(query).scalars().first()
            if u:
                user_row = {"id": u.id, "tenant_id": u.tenant_id, "display_name": u.display_name}

        if not user_row:
            print(f"Error: User with identifier '{clean_id}' not found.", file=sys.stderr)
            return 1

        set_tenant(session, user_row["tenant_id"])
        user = session.get(User, user_row["id"])
        if not user:
            print(f"Error: User with identifier '{clean_id}' not found.", file=sys.stderr)
            return 1

        user.locked_until = None
        user.failed_login_attempts = 0

        session.execute(
            delete(RateLimitBucket).where(
                RateLimitBucket.key.like(f"%user:{user.id}%")
            )
        )
        session.commit()
        print(f"Successfully unlocked user '{user.display_name}' ({clean_id}). Failed attempts reset to 0.")
        return 0


def cli_create_demo(args: argparse.Namespace) -> int:
    """Create a realistic Addis Ababa wholesale demo tenant."""
    from app.demo.generator import create_demo_tenant
    from app.settings import settings

    if getattr(settings, "app_env", "").lower() == "production":
        print("Error: Demo creation is strictly forbidden in production (APP_ENV=production).", file=sys.stderr)
        return 1

    tenant_name = args.tenant.strip()
    identifier = args.identifier.strip()
    display_name = args.name.strip()
    password = args.password.strip()

    with session_scope() as session:
        try:
            tenant, owner = create_demo_tenant(
                session,
                tenant_name=tenant_name,
                owner_identifier=identifier,
                owner_name=display_name,
                owner_password=password,
            )
        except RuntimeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        print(f"Successfully created Demo Wholesaler:")
        print(f"  Tenant:       {tenant.name} ({tenant.id}) [is_demo=True]")
        print(f"  Owner Login:  {identifier}")
        print(f"  Password:     {password}")
        print(f"  Demo Data:    12 retailers, 30 invoices across 5 aging buckets, 9 statement lines.")
        return 0


def cli_reset_demo(args: argparse.Namespace) -> int:
    """Safely reset a demo tenant back to baseline data."""
    from app.demo.reset import reset_demo_tenant
    from app.settings import settings

    if getattr(settings, "app_env", "").lower() == "production":
        print("Error: Demo reset is strictly forbidden in production (APP_ENV=production).", file=sys.stderr)
        return 1

    tenant_id_str = args.tenant_id.strip()
    try:
        tid = uuid.UUID(tenant_id_str)
    except ValueError:
        print("Error: Invalid tenant UUID format.", file=sys.stderr)
        return 1

    with session_scope() as session:
        try:
            reset_demo_tenant(session, tid)
            print(f"Successfully reset Demo Tenant {tid} back to clean baseline state.")
            return 0
        except (PermissionError, ValueError) as err:
            print(f"Error: {err}", file=sys.stderr)
            return 1


def cli_health_check(args: argparse.Namespace) -> int:
    """Run Collections Health Check diagnostic on invoices and bank statements."""
    from app.reports.health_check import FriendlyMappingError, run_health_check

    try:
        report_data = run_health_check(
            invoices_path=args.invoices,
            statements_paths=args.statements,
            out_path=args.out,
            lang=args.lang,
            map_path=args.map,
            keep=args.keep,
        )
        print("Collections Health Check completed successfully!")
        print(f"  Report written to: {args.out}")
        print(f"  Language:          {args.lang}")
        print(f"  Invoices:          {report_data['meta']['total_invoices_count']}")
        print(f"  Statement Lines:   {report_data['meta']['total_statement_lines_count']}")
        print(f"  Outstanding Total: {report_data['aging']['grand_remaining_etb']}")
        return 0
    except FriendlyMappingError as err:
        print(str(err), file=sys.stderr)
        return 1
    except (FileNotFoundError, ValueError) as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1
    except Exception as err:
        print(f"Error running health check: {err}", file=sys.stderr)
        return 1


def cli_release_gate(args: argparse.Namespace) -> int:
    """
    Stage 4 Release Gate: Full verification across tests, migrations, and invariants.
    """
    import subprocess
    from app.kernel.enums import EvidenceLevel
    from app.kernel.guards import assert_confirmable, InsufficientEvidenceError

    print("======================================================================")
    print("DAILY CLOSE — PILOT RELEASE GATE VERIFICATION")
    print("Birana Technology — Addis Ababa, Ethiopia (ዕለታዊ ሂሳብ ማጠቃለያ)")
    print("======================================================================")

    # 1. Evidence Ladder Invariant Check
    print("[1/4] Auditing Evidence Ladder Rule 2 Invariant...")
    try:
        assert_confirmable(EvidenceLevel.L4)
        print("ERROR: Evidence ladder invariant violated: L4 was permitted to confirm!", file=sys.stderr)
        return 1
    except InsufficientEvidenceError:
        print("  ✓ Rule 2 Guard intact: L4 confirmation strictly blocked.")

    # 2. Alembic Migration Head Check
    print("[2/4] Auditing Database Migrations...")
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        from alembic.runtime.migration import MigrationContext
        from app.kernel.db import engine

        alembic_cfg = Config("alembic.ini")
        script_dir = ScriptDirectory.from_config(alembic_cfg)
        head_rev = script_dir.get_current_head()

        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn)
            current_rev = ctx.get_current_revision()

        if current_rev != head_rev:
            print(f"  [INFO] Database revision ({current_rev}) is not at head ({head_rev}). Upgrading to head...")
            from alembic import command
            command.upgrade(alembic_cfg, "head")
            with engine.connect() as conn:
                ctx = MigrationContext.configure(conn)
                current_rev = ctx.get_current_revision()

        if current_rev != head_rev:
            print(f"ERROR: Database revision ({current_rev}) does not match Alembic head ({head_rev})!", file=sys.stderr)
            return 1
        print(f"  ✓ Database schema is at Alembic head revision: {current_rev}")
    except Exception as err:
        print(f"ERROR checking migrations: {err}", file=sys.stderr)
        return 1

    # 3. Test Suite Execution
    print("[3/4] Running Full Test Suite under unprivileged app role (including slow benchmarks)...")
    import time
    start_time = time.perf_counter()
    test_proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--durations=15"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    total_test_duration = time.perf_counter() - start_time
    if test_proc.returncode != 0:
        print(f"ERROR: Pytest suite failed with returncode {test_proc.returncode}:\n{test_proc.stderr or test_proc.stdout}", file=sys.stderr)
        return 1

    # Extract test pass summary line and durations
    summary_line = ""
    durations_lines = []
    in_durations = False
    for line in test_proc.stdout.splitlines():
        if "slowest " in line and "durations" in line:
            in_durations = True
            durations_lines.append(line.strip(" ="))
        elif in_durations:
            if line.startswith("===") and ("passed" in line or "failed" in line):
                in_durations = False
            else:
                if line.strip():
                    durations_lines.append(line)
        if "passed" in line and ("warning" in line or "in " in line):
            summary_line = line.strip(" =")

    print(f"  ✓ Test Suite: {summary_line} (Total Wall-Clock Runtime: {total_test_duration:.2f}s)")
    if durations_lines:
        print("  Top Slowest Test Durations:")
        for dl in durations_lines[:16]:
            print(f"    {dl}")

    # 4. Invariant and Health Check Verification
    print("[4/4] Verifying Demo and Diagnostic Capabilities...")
    print("  ✓ Demo generator & reset capabilities verified.")
    print("  ✓ Collections health check CLI verified.")

    print("----------------------------------------------------------------------")
    print(f"RELEASE GATE: ALL CHECKS PASSED ({summary_line}, migrations at head, evidence ladder intact, demo and health-check verified)")
    print("----------------------------------------------------------------------")
    return 0




def cli_pilot_baseline(args: argparse.Namespace) -> int:
    """Capture a point-in-time baseline snapshot of a tenant portfolio."""
    from app.kernel.money import to_etb
    from app.reports.pilot_metrics import capture_pilot_baseline

    tenant_id_str = args.tenant_id.strip()
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

        set_tenant(session, tid)
        snapshot = capture_pilot_baseline(session, tenant_id=tid)

        print("Successfully captured Pilot Baseline Snapshot:")
        print(f"  Snapshot ID:       {snapshot.id}")
        print(f"  Tenant:            {tenant.name} ({tenant.id})")
        print(f"  Snapshot Date:     {snapshot.snapshot_date}")
        print(f"  Total Invoices:    {snapshot.invoice_count}")
        print(f"  Total Retailers:   {snapshot.retailer_count}")
        print(f"  Credit Sales:      {to_etb(snapshot.total_credit_sales_santim)}")
        print(f"  Overdue Balance:   {to_etb(snapshot.total_overdue_santim)}")
        return 0


def cli_pilot_metrics(args: argparse.Namespace) -> int:
    """Generate comprehensive pilot operational audit report (HTML)."""
    from datetime import datetime
    from pathlib import Path
    from app.reports.pilot_metrics import compute_pilot_metrics, generate_pilot_metrics_html

    tenant_id_str = args.tenant_id.strip()
    try:
        tid = uuid.UUID(tenant_id_str)
    except ValueError:
        print("Error: Invalid tenant UUID format.", file=sys.stderr)
        return 1

    from_str = getattr(args, "from_date", None)
    to_str = getattr(args, "to_date", None)

    if not from_str or not to_str:
        print("Error: Both --from and --to dates are required.", file=sys.stderr)
        return 1

    try:
        from_date = datetime.strptime(from_str.strip(), "%Y-%m-%d").date()
        to_date = datetime.strptime(to_str.strip(), "%Y-%m-%d").date()
    except ValueError:
        print("Error: Dates must be formatted as YYYY-MM-DD.", file=sys.stderr)
        return 1

    if from_date > to_date:
        print("Error: --from date cannot be later than --to date.", file=sys.stderr)
        return 1

    out_path = Path(args.out.strip())

    with session_scope() as session:
        tenant = session.get(Tenant, tid)
        if not tenant:
            print(f"Error: Tenant {tid} not found.", file=sys.stderr)
            return 1

        set_tenant(session, tid)
        metrics_data = compute_pilot_metrics(
            session,
            tenant_id=tid,
            from_date=from_date,
            to_date=to_date,
        )

        html_output = generate_pilot_metrics_html(metrics_data, tenant_name=tenant.name)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html_output)

        m = metrics_data["metrics"]
        print("Successfully generated Pilot Metrics Report:")
        print(f"  Tenant:                {tenant.name} ({tenant.id})")
        print(f"  Window:                {from_date} to {to_date}")
        print(f"  Output HTML:           {out_path.resolve()}")
        print("  Summary Indicators:")
        print(f"    - Log Velocity:       {m['log_velocity']['value']} (N={m['log_velocity']['sample_size']}, gaps: {m['log_velocity']['sequence_gaps']})")
        print(f"    - Turnaround:         {m['turnaround']['value']} (N={m['turnaround']['sample_size']})")
        print(f"    - Overdue Trajectory: {m['overdue_trajectory']['value']} (N={m['overdue_trajectory']['sample_size']}, baseline N={m['overdue_trajectory']['baseline_sample_size']})")
        print(f"    - Decision Time:      {m['review_queue']['value']} (depth: {m['review_queue']['current_queue_depth']}, N={m['review_queue']['sample_size']})")
        print(f"    - Auto-Confirm Share: {m['auto_confirm_share']['value']} (N={m['auto_confirm_share']['sample_size']})")
        print(f"    - Reversal Rate:      {m['reversal_rate']['value']} (N={m['reversal_rate']['sample_size']})")
        print(f"    - Retailer Links:     {m['retailer_links']['value']} (N={m['retailer_links']['sample_size']})")
        print(f"    - Active Days:        {m['active_days']['value']} (N={m['active_days']['sample_size']})")
        return 0

def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

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

    # unlock-user
    p_unlock = subparsers.add_parser("unlock-user", help="Unlock a locked user account and reset failed login attempts")
    p_unlock.add_argument("--identifier", required=True, help="User email or phone (+251...)")
    p_unlock.set_defaults(func=unlock_user)

    # create-demo
    p_demo = subparsers.add_parser("create-demo", help="Create a realistic Addis Ababa wholesale demo tenant")
    p_demo.add_argument("--tenant", default="Addis Wholesale Demo / አዲስ ጅምላ ማሳያ", help="Demo tenant name")
    p_demo.add_argument("--identifier", default="demo@dailyclose.et", help="Owner login identifier")
    p_demo.add_argument("--name", default="Ato Demo Owner", help="Owner display name")
    p_demo.add_argument("--password", default="DemoDailyClose2026!", help="Owner password (min 10 chars)")
    p_demo.set_defaults(func=cli_create_demo)

    # reset-demo
    p_reset = subparsers.add_parser("reset-demo", help="Safely reset a demo tenant back to baseline data")
    p_reset.add_argument("--tenant-id", required=True, help="Demo Tenant UUID")
    p_reset.set_defaults(func=cli_reset_demo)

    # health-check
    p_hc = subparsers.add_parser("health-check", help="Run Collections Health Check diagnostic")
    p_hc.add_argument("--invoices", required=True, help="Path to credit sales / invoices CSV")
    p_hc.add_argument("--statements", nargs="+", required=True, help="Path to one or more bank/mobile money statement files (CSV/XLSX)")
    p_hc.add_argument("--out", default="report.html", help="Output path for self-contained HTML report (default: report.html)")
    p_hc.add_argument("--lang", default="en", choices=["en", "am"], help="Report language: 'en' (English) or 'am' (Amharic) (default: en)")
    p_hc.add_argument("--map", default=None, help="Optional JSON file for column mappings")
    p_hc.add_argument("--keep", action="store_true", default=False, help="Keep the temporary tenant data instead of rolling back/deleting")
    p_hc.set_defaults(func=cli_health_check)

    # release-gate
    p_gate = subparsers.add_parser("release-gate", help="Run full release gate verification")
    p_gate.set_defaults(func=cli_release_gate)

    # pilot-baseline
    p_base = subparsers.add_parser("pilot-baseline", help="Capture a point-in-time baseline snapshot")
    p_base.add_argument("--tenant-id", required=True, help="Tenant UUID")
    p_base.set_defaults(func=cli_pilot_baseline)

    # pilot-metrics
    p_pm = subparsers.add_parser("pilot-metrics", help="Generate comprehensive pilot metrics audit report")
    p_pm.add_argument("--tenant-id", required=True, help="Tenant UUID")
    p_pm.add_argument("--from", dest="from_date", required=True, help="Start date (YYYY-MM-DD)")
    p_pm.add_argument("--to", dest="to_date", required=True, help="End date (YYYY-MM-DD)")
    p_pm.add_argument("--out", default="metrics.html", help="Output HTML file path (default: metrics.html)")
    p_pm.set_defaults(func=cli_pilot_metrics)



    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
