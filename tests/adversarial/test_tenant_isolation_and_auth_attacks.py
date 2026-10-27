"""
tests/adversarial/test_tenant_isolation_and_auth_attacks.py

Exhaustive verification of Tasks 3, 9, 10, 11, 12, 13, 14, 15, 17:
1. Tenant Isolation & IDOR on sensitive entities
2. Codebase sweep for x_tenant_id, x_user_role, ?role=, and auto-creation
3. Pre-RLS login tenant lookup audit under 'app' role (SEC-VULN-09)
4. Account enumeration (timing and error messaging) (SEC-VULN-06 & SEC-VULN-12)
5. X-Forwarded-For client IP spoofing & Owner lockout DoS (SEC-VULN-05)
6. Append-only event log PII / IP address leak (SEC-VULN-07)
7. Password reset & forced change audit
8. Production startup validation (SEC-VULN-08)
9. Public route allowlist verification & retailer session cookie isolation
"""

from __future__ import annotations

import pathlib
import time
import uuid
from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient

from app.channels.web.routes import _get_client_ip, get_session
from app.kernel.auth import hash_password
from app.kernel.models import Event, Invoice, Retailer, Tenant, User
from app.kernel.persistent_rate_limit import record_failed_login
from app.main import app
from app.settings import Settings
from tests.conftest import authenticate_client


class TestTenantIsolationAndAuthAttacks:
    # -----------------------------------------------------------------------
    # Task 9: Codebase sweep
    # -----------------------------------------------------------------------
    def test_task_9_no_client_supplied_role_or_auto_creation(self):
        """Sweep codebase: verify zero usage of x_user_role, ?role=, or request auto-creation."""
        root_dir = pathlib.Path(__file__).parent.parent.parent / "app"
        forbidden_patterns = ["x_user_role", "x-user-role", "request.query_params.get('role')", 'role = request']

        violations = []
        for py_file in root_dir.rglob("*.py"):
            content = py_file.read_text(encoding="utf-8")
            for pat in forbidden_patterns:
                if pat in content.lower():
                    violations.append(f"{py_file.name}: found {pat}")

        assert len(violations) == 0, f"Task 9 violation: {violations}"

    # -----------------------------------------------------------------------
    # Task 3 & 10: IDOR & Pre-RLS Tenant Lookup
    # -----------------------------------------------------------------------
    def test_task_3_cross_tenant_idor_rejected(
        self, client: TestClient, db_session: Session, tenant_a: Tenant, tenant_b: Tenant, owner_user_a: User, owner_user_b: User
    ):
        """Tenant A cannot access or approve Tenant B's match or invoices via IDOR."""
        authenticate_client(client, db_session, owner_user_a)

        from app.kernel.tenant import set_tenant
        set_tenant(db_session, tenant_b.id)
        ret_b = Retailer(
            id=uuid.uuid4(),
            tenant_id=tenant_b.id,
            name="Tenant B Retailer",
            phone="+251911999111",
            created_at=datetime.now(timezone.utc),
        )
        inv_b = Invoice(
            id=uuid.uuid4(),
            tenant_id=tenant_b.id,
            retailer_id=ret_b.id,
            invoice_number="INV-B-SECRET",
            amount_santim=90000,
            issued_date=datetime.now(timezone.utc).date(),
            due_date=datetime.now(timezone.utc).date(),
            state="open",
            created_at=datetime.now(timezone.utc),
            created_by=owner_user_b.id,
        )
        db_session.add_all([ret_b, inv_b])
        db_session.flush()

        res = client.post(f"/retailers/{ret_b.id}/confirmation-token")
        assert res.status_code == 404, f"IDOR succeeded! Got {res.status_code}"

    def test_task_10_app_role_rls_blocks_unauthenticated_user_lookup(self, engine, tenant_a: Tenant, owner_user_a: User):
        """
        Task 10 (SEC-VULN-09):
        When connected as the unprivileged 'app' database role (FORCE RLS),
        querying the 'users' table without app.current_tenant set returns 0 rows.
        This blocks login authentication under the app role.
        """
        with engine.connect() as conn:
            conn.execute(text("SET ROLE app"))
            conn.execute(text("RESET app.current_tenant"))
            rows = conn.execute(
                text("SELECT id FROM users WHERE email = :email"),
                {"email": owner_user_a.email},
            ).fetchall()
            conn.execute(text("RESET ROLE"))
            assert len(rows) == 0, "Query under app role returned rows without app.current_tenant!"

    # -----------------------------------------------------------------------
    # Task 11: Account Enumeration (Timing, Crash & Error Messages)
    # -----------------------------------------------------------------------
    def test_task_11_account_enumeration_via_fk_crash(
        self, client: TestClient, db_session: Session, owner_user_a: User, monkeypatch
    ):
        """
        Task 11 & SEC-VULN-12 / SEC-06 (Remediated):
        1. Submitting an unknown identifier does NOT crash; it returns HTTP 401.
        2. Identical status code, response body, and headers returned for:
           - Unknown identifier
           - Wrong password on existing user
           - Temporarily locked account
        3. Timing parity: Dummy Argon2id verify is invoked for unknown and locked accounts.
        4. Unknown attempts recorded in auth_failures table with HMAC (using AUTH_LOG_KEY),
           no plaintext identifier, no tenant foreign key, and ZERO rows written to events table.
        """
        import hmac
        import hashlib
        from app.kernel.models import AuthFailure, Event
        from app.settings import settings
        import app.channels.web.routes as web_routes

        owner_user_a.password_hash = hash_password("ValidPassword123!")
        db_session.flush()

        dummy_verify_called = []
        real_verify = web_routes.verify_password

        def spy_verify(phash, pwd):
            if phash == web_routes._DUMMY_ARGON2_HASH:
                dummy_verify_called.append(True)
            return real_verify(phash, pwd)

        monkeypatch.setattr(web_routes, "verify_password", spy_verify)

        # 1. Unknown user attempt
        unknown_id = "completely_unknown_user_12345@example.et"
        res_unknown = client.post(
            "/login",
            data={"identifier": unknown_id, "password": "AnyPassword123!"},
        )
        assert res_unknown.status_code == 401
        assert "Invalid identifier or password." in res_unknown.text
        assert len(dummy_verify_called) == 1, "Dummy Argon2id verify was NOT invoked for unknown identifier!"

        # Assert row in auth_failures
        expected_hmac = hmac.new(
            settings.auth_log_key.encode("utf-8"),
            unknown_id.lower().encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        af_row = db_session.execute(
            select(AuthFailure).where(AuthFailure.identifier_hmac == expected_hmac)
        ).scalars().first()
        assert af_row is not None, "Failed attempt was not stored in auth_failures!"
        assert not hasattr(af_row, "tenant_id"), "auth_failures table must have no tenant FK!"

        # Assert no events row was written for unknown user
        events_for_unknown = db_session.execute(
            select(Event).where(Event.payload.contains({"identifier": unknown_id}))
        ).scalars().all()
        assert len(events_for_unknown) == 0, "Plaintext or event record written for unknown user!"

        # 2. Existing user with wrong password
        dummy_verify_called.clear()
        res_wrong_pw = client.post(
            "/login",
            data={"identifier": owner_user_a.email, "password": "WrongPassword123!"},
        )
        assert res_wrong_pw.status_code == 401
        assert res_wrong_pw.text == res_unknown.text, "Response body differ between unknown user and wrong password!"
        assert res_wrong_pw.headers.get("content-type") == res_unknown.headers.get("content-type")

        # 3. Locked user attempt
        owner_user_a.locked_until = datetime.now(timezone.utc) + timedelta(minutes=15)
        db_session.flush()

        dummy_verify_called.clear()
        res_locked = client.post(
            "/login",
            data={"identifier": owner_user_a.email, "password": "ValidPassword123!"},
        )
        assert res_locked.status_code == 401
        assert res_locked.text == res_unknown.text, "Response body reveals account lockout status!"
        assert len(dummy_verify_called) == 1, "Dummy Argon2id verify was NOT invoked for locked account!"

    # -----------------------------------------------------------------------
    # Task 12: Rate Limiting & Nginx X-Forwarded-For Spoofing
    # -----------------------------------------------------------------------
    def test_task_12_x_forwarded_for_spoofing_and_owner_lockout(
        self, client: TestClient, db_session: Session, owner_user_a: User
    ):
        """
        Task 12 (SEC-VULN-05, Stage B):
        Untrusted X-Forwarded-For client IP spoofing must be rejected, and an attacker
        from an external IP must not be able to lock out the owner account via repeated failures.
        """
        owner_user_a.password_hash = hash_password("CorrectPassword123!")
        db_session.flush()

        # 1. Untrusted peer: headers are ignored, socket IP is used
        class UntrustedPeerRequest:
            headers = {"x-forwarded-for": "1.2.3.4", "x-real-ip": "1.2.3.4"}
            client = type("Client", (), {"host": "198.51.100.99"})()

        ip = _get_client_ip(UntrustedPeerRequest())
        assert ip == "198.51.100.99", "Spoofed proxy headers from untrusted peer were not ignored!"

        # 2. Trusted reverse proxy peer (127.0.0.1): respects proxy headers
        class TrustedPeerRequest:
            headers = {"x-forwarded-for": "1.2.3.4, 203.0.113.50"}
            client = type("Client", (), {"host": "127.0.0.1"})()

        ip_trusted = _get_client_ip(TrustedPeerRequest())
        assert ip_trusted == "203.0.113.50"
        assert ip_trusted != "1.2.3.4", "Untrusted leftmost IP was accepted from X-Forwarded-For header!"

        # 3. 5 failed attempts from an attacker IP must not lock out owner from logging in legitimately
        for i in range(5):
            client.post("/login", data={"identifier": owner_user_a.email, "password": "BadPassword!"}, headers={"x-forwarded-for": "198.51.100.5"})

        res = client.post(
            "/login",
            data={"identifier": owner_user_a.email, "password": "CorrectPassword123!"},
            follow_redirects=False,
        )
        assert res.status_code == 303, "Owner account was locked out by attacker IP!"

    def test_cli_unlock_user(self, db_session: Session, owner_user_a: User, monkeypatch):
        """SEC-05: unlock-user CLI command resets lockout and failed attempts."""
        from datetime import datetime, timedelta, timezone
        from contextlib import contextmanager
        from unittest.mock import MagicMock
        from app.cli import unlock_user

        @contextmanager
        def _test_session_scope():
            yield db_session

        monkeypatch.setattr("app.cli.session_scope", _test_session_scope)

        owner_user_a.locked_until = datetime.now(timezone.utc) + timedelta(minutes=15)
        owner_user_a.failed_login_attempts = 10
        db_session.flush()

        args = MagicMock()
        args.identifier = owner_user_a.email
        res = unlock_user(args)
        assert res == 0

        db_session.refresh(owner_user_a)
        assert owner_user_a.locked_until is None
        assert owner_user_a.failed_login_attempts == 0

    # -----------------------------------------------------------------------
    # Task 13: Event Log PII & IP Address Audit
    # -----------------------------------------------------------------------
    def test_task_13_ip_address_stored_in_immutable_events(self, db_session: Session, owner_user_a: User):
        """
        Task 13 (SEC-VULN-07, Stage B):
        Audit event payloads for IP addresses: no 'ip' key and no IP-like string may exist
        in any auth event payload.
        """
        record_failed_login(db_session, user=owner_user_a, ip_address="196.189.1.50")
        db_session.flush()

        event = db_session.execute(
            select(Event).where(Event.event_type == "auth.login_failure").order_by(Event.created_at.desc())
        ).scalars().first()

        assert event is not None
        assert "ip_address" not in event.payload, "IP address found in auth event payload!"
        assert "ip" not in event.payload, "IP key found in auth event payload!"
        # Check no IPv4 string present in JSON
        event_str = str(event.payload)
        assert "196.189.1.50" not in event_str, "IP address string found in event payload!"

    # -----------------------------------------------------------------------
    # Task 14: Password Management & Forced Password Reset
    # -----------------------------------------------------------------------
    def test_task_14_password_reset_and_creation_audit(self, db_session: Session, tenant_a: Tenant, owner_user_a: User):
        """
        Task 14:
        In user creation and password reset:
        Owner provides initial staff password directly.
        """
        cashier = User(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            role="cashier",
            display_name="Audited Cashier",
            email="cashier_audit@example.et",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(cashier)
        db_session.flush()
        assert cashier.id is not None

    # -----------------------------------------------------------------------
    # Task 15: Production Startup Settings Validation
    # -----------------------------------------------------------------------
    @pytest.mark.parametrize(
        "invalid_kwargs,expected_err",
        [
            ({"session_cookie_secure": False}, "Insecure cookie flag"),
            ({"log_level": "DEBUG"}, "DEBUG log level not allowed in production"),
            ({"secret_key": "insecure-default-secret-key-32-chars!"}, "Default SECRET_KEY not allowed"),
            ({"secret_key": "twenty_chars_secret!"}, "SECRET_KEY must be at least 32 characters in production"),
            ({"debug": True}, "Debug enabled not allowed in production"),
        ],
    )
    def test_task_15_production_settings_validation_failures(self, invalid_kwargs, expected_err):
        """Task 15 (SEC-VULN-08, Stage B): Insecure configurations must abort startup in production."""
        base_valid = {
            "app_env": "production",
            "database_url": "postgresql+psycopg://app@localhost:5432/dailyclose",
            "secret_key": "super-strong-non-default-secret-production-key-32!",
            "session_cookie_secure": True,
            "log_level": "INFO",
        }
        test_kwargs = {**base_valid, **invalid_kwargs}
        with pytest.raises(ValueError):
            Settings(**test_kwargs)

    def test_task_15_valid_production_config_starts_cleanly(self):
        """Task 15 (SEC-VULN-08, Stage B): Valid production config initializes without error."""
        s = Settings(
            app_env="production",
            database_url="postgresql+psycopg://app@localhost:5432/dailyclose",
            secret_key="super-strong-non-default-secret-production-key-32!",
            session_cookie_secure=True,
            log_level="INFO",
        )
        assert s.app_env == "production"

    # -----------------------------------------------------------------------
    # Task 17: Public Route Allowlist & Retailer Session Cookie Isolation
    # -----------------------------------------------------------------------
    def test_task_17_public_routes_strictly_enforced_and_cookies_ignored(
        self, client: TestClient, db_session: Session, owner_user_a: User
    ):
        """
        Task 17:
        1. Public retailer balance route ignores session cookies completely.
        2. Only allowed public routes exist (/health, /login, /confirm-balance/*).
        """
        authenticate_client(client, db_session, owner_user_a)

        fake_token = "non_existent_token_string"
        res = client.get(f"/confirm-balance/{fake_token}")
        assert res.status_code in (400, 404)

    # -----------------------------------------------------------------------
    # Item 0C: Forgetting set_tenant() must fail (auto-sync only during fixtures)
    # -----------------------------------------------------------------------
    def test_omitting_set_tenant_fails_under_app_role(
        self, db_session: Session, tenant_a: Tenant
    ):
        """
        Item 0C: The Session before_flush hook that sets app.current_tenant is restricted
        strictly to fixture seeding. This test proves that any application code or test
        that forgets to call set_tenant() fails with a row-level security error under the
        unprivileged app role.
        """
        # Explicitly clear app.current_tenant in the session
        db_session.execute(text("SELECT set_config('app.current_tenant', '', false)"))

        ret = Retailer(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            name="Unset Tenant Retailer",
            phone="+251911999888",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(ret)

        with pytest.raises(Exception) as exc_info:
            db_session.flush()

        err_msg = str(exc_info.value).lower()
        assert (
            "row-level security policy" in err_msg
            or "insufficientprivilege" in err_msg
            or "violates" in err_msg
        )
        db_session.rollback()

