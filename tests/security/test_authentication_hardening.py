"""
tests/security/test_authentication_hardening.py

Exhaustive verification of authentication and tenant hardening:
1. Tenant Identity Cannot Be Spoofed:
   - Forged X-Tenant-ID headers, query params, or body fields are ignored or rejected.
2. Cashier Role Restrictions:
   - Cashier cannot call any owner-only route or Telegram handler.
3. Automated Route Sweep:
   - Every non-public route automatically rejects unauthenticated requests with HTTP 401.
4. CSRF Protection:
   - State-changing requests without a valid CSRF synchronizer token return HTTP 403.
5. Session Lifecycle:
   - Idle timeout, absolute expiry, logout, and token reuse rejection.
6. Persistent Rate Limiting & Account Lockout:
   - Survives restarts and multi-worker setups (persisted in PostgreSQL).
   - 5 consecutive failures triggers 15-minute account lockout.
7. Cross-Tenant Data Isolation:
   - Guessing IDs of another tenant returns HTTP 404 / access denied.
8. Deactivated User Immediate Revocation:
   - Deactivating a user immediately terminates their active sessions and blocks requests.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.channels.telegram.auth import generate_invite_code, redeem_invite_code
from app.channels.telegram.bot import handle_approve_callback, handle_review, handle_unverified_callback
from app.channels.web.auth_deps import SESSION_COOKIE_NAME
from app.channels.web.routes import get_session
from app.kernel.auth import (
    AccountLockedError,
    create_user_session,
    generate_csrf_token,
    hash_password,
    terminate_session,
    validate_session_token,
)
from app.kernel.enums import EvidenceLevel, InvoiceState, MatchState, PaymentState, UserRole
from app.kernel.models import (
    ImportBatch,
    Invoice,
    PaymentLine,
    PaymentMatch,
    RateLimitBucket,
    Retailer,
    Session as SessionModel,
    Tenant,
    User,
)
from app.kernel.persistent_rate_limit import (
    check_persistent_rate_limit,
    record_failed_login,
    record_successful_login,
)
from app.kernel.sales import record_credit_sale
from app.kernel.tenant import set_tenant
from app.main import app
from app.settings import settings
from tests.conftest import authenticate_client


@pytest.fixture
def client(db_session: Session):
    def _override_get_session():
        yield db_session

    app.dependency_overrides[get_session] = _override_get_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 1. Tenant Identity Cannot Be Spoofed
# ---------------------------------------------------------------------------
class TestTenantSpoofingResistance:
    def test_forged_header_and_query_param_ignored(
        self,
        client: TestClient,
        db_session: Session,
        tenant_a: Tenant,
        tenant_b: Tenant,
        owner_user_a: User,
    ):
        """Passing X-Tenant-ID header or ?tenant_id= pointing to Tenant B has zero effect."""
        set_tenant(db_session, tenant_b.id)
        now = datetime.now(timezone.utc)
        ret_b = Retailer(id=uuid.uuid4(), tenant_id=tenant_b.id, name="Secret Retailer B", created_at=now)
        db_session.add(ret_b)
        db_session.flush()

        inv_b = Invoice(
            id=uuid.uuid4(),
            tenant_id=tenant_b.id,
            retailer_id=ret_b.id,
            invoice_number="INV-SECRET-B-999",
            amount_santim=999900,
            issued_date=now.date(),
            due_date=now.date() + timedelta(days=30),
            created_at=now,
            created_by=owner_user_a.id,
        )
        db_session.add(inv_b)
        db_session.commit()

        set_tenant(db_session, tenant_a.id)
        authenticate_client(client, db_session, owner_user_a)

        # Attempt to access dashboard with Tenant B header and query param
        resp = client.get(f"/?tenant_id={tenant_b.id}", headers={"X-Tenant-ID": str(tenant_b.id)})
        assert resp.status_code == 200
        # Must not contain Tenant B data
        assert "INV-SECRET-B-999" not in resp.text
        assert "Secret Retailer B" not in resp.text
        assert tenant_a.name in resp.text

    def test_sale_creation_cannot_inject_foreign_tenant(
        self,
        client: TestClient,
        db_session: Session,
        tenant_a: Tenant,
        tenant_b: Tenant,
        owner_user_a: User,
    ):
        """Submitting tenant_id=Tenant B in form payload does not cross tenants."""
        now = datetime.now(timezone.utc)
        ret_a = Retailer(id=uuid.uuid4(), tenant_id=tenant_a.id, name="Retailer A", created_at=now)
        db_session.add(ret_a)
        db_session.flush()

        authenticate_client(client, db_session, owner_user_a)

        data = {
            "invoice_number": "INV-SPOOF-TEST",
            "retailer_id": str(ret_a.id),
            "amount_etb": "5000.00",
            "due_date": "2026-12-31",
            "tenant_id": str(tenant_b.id),  # Attempted injection
        }
        resp = client.post("/sales", data=data, follow_redirects=False)
        assert resp.status_code == 303

        # Invariant: Invoice must strictly be created under tenant_a
        inv = db_session.execute(
            select(Invoice).where(Invoice.invoice_number == "INV-SPOOF-TEST")
        ).scalar_one()
        assert inv.tenant_id == tenant_a.id
        assert inv.tenant_id != tenant_b.id


# ---------------------------------------------------------------------------
# 2. Cashier Role Restrictions
# ---------------------------------------------------------------------------
class TestCashierRoleRestrictions:
    def test_cashier_blocked_from_all_owner_only_routes(
        self,
        client: TestClient,
        db_session: Session,
        tenant_a: Tenant,
        cashier_user_a: User,
    ):
        """Cashier must receive HTTP 403 on every owner-only web endpoint."""
        authenticate_client(client, db_session, cashier_user_a)
        dummy_id = uuid.uuid4()

        # 1. Statement upload
        files = {"file": ("test.csv", b"dummy,csv", "text/csv")}
        resp = client.post("/statements/upload", data={"source_name": "cbe_csv"}, files=files)
        assert resp.status_code == 403, "Cashier should not upload statements"

        # 2. Match approval
        resp = client.post(f"/matches/{dummy_id}/approve")
        assert resp.status_code == 403, "Cashier should not approve matches"

        # 3. Mark unverified (L4 override)
        resp = client.post(f"/matches/{dummy_id}/mark-unverified")
        assert resp.status_code == 403, "Cashier should not mark unverified"

        # 4. Match rejection
        resp = client.post(f"/matches/{dummy_id}/reject")
        assert resp.status_code == 403, "Cashier should not reject matches"

        # 5. Full sales CSV export
        resp = client.get("/exports/sales.csv")
        assert resp.status_code == 403, "Cashier should not download sales export"

        # 6. Full payments CSV export
        resp = client.get("/exports/payments.csv")
        assert resp.status_code == 403, "Cashier should not download payments export"

        # 7. Accountant CSV export
        resp = client.get("/exports/accountant.csv")
        assert resp.status_code == 403, "Cashier should not download accountant export"

        # 8. User management listing
        resp = client.get("/settings/users")
        assert resp.status_code == 403, "Cashier should not list users"

        # 9. Provision user
        user_data = {
            "display_name": "Sub User",
            "role": "cashier",
            "password": "ValidPassword123!",
            "email": "test@alpha.et",
        }
        resp = client.post("/settings/users", data=user_data)
        assert resp.status_code == 403, "Cashier should not create users"

        # 10. Reset password
        resp = client.post(f"/settings/users/{dummy_id}/reset-password", data={"new_password": "NewValidPassword123!"})
        assert resp.status_code == 403, "Cashier should not reset passwords"

        # 11. Deactivate user
        resp = client.post(f"/settings/users/{dummy_id}/deactivate")
        assert resp.status_code == 403, "Cashier should not deactivate users"

    @pytest.mark.asyncio
    async def test_telegram_cashier_blocked_from_owner_actions(
        self,
        db_session: Session,
        tenant_a: Tenant,
        cashier_user_a: User,
        monkeypatch,
    ):
        """Telegram cashier role cannot trigger match approval or review queue."""
        from contextlib import contextmanager
        from unittest.mock import AsyncMock, MagicMock

        @contextmanager
        def _test_session_scope():
            yield db_session

        monkeypatch.setattr("app.channels.telegram.bot.get_session_scope", _test_session_scope)

        # 1. Callback query mock for approve
        callback_query = MagicMock()
        callback_query.data = f"approve:{uuid.uuid4()}"
        callback_query.from_user.id = 777888
        callback_query.answer = AsyncMock()
        cashier_user_a.telegram_id = 777888
        db_session.flush()

        await handle_approve_callback(callback_query)
        callback_query.answer.assert_called_with("Only owners can approve payment matches.", show_alert=True)

        # 2. Callback query mock for mark_unverified
        callback_query_unv = MagicMock()
        callback_query_unv.data = f"unverified:{uuid.uuid4()}"
        callback_query_unv.from_user.id = 777888
        callback_query_unv.answer = AsyncMock()

        await handle_unverified_callback(callback_query_unv)
        callback_query_unv.answer.assert_called_with("✕ Permission denied: Only owners can mark L4 items as accepted_unverified.", show_alert=True)

        # 3. Message mock for /review
        message = MagicMock()
        message.from_user.id = 777888
        message.answer = AsyncMock()

        await handle_review(message)
        message.answer.assert_called_with("Only owners can review proposed matches.")


# ---------------------------------------------------------------------------
# 3. Automated Route Sweep for Unauthenticated Protection
# ---------------------------------------------------------------------------
class TestAutomatedRouteAuthSweep:
    PUBLIC_PATHS = {
        "/health": {"GET"},
        "/login": {"GET", "POST"},
        "/confirm-balance/{raw_token}": {"GET"},
        "/confirm-balance/{raw_token}/confirm": {"POST"},
        "/confirm-balance/{raw_token}/dispute": {"POST"},
    }

    def test_all_non_public_routes_strictly_reject_unauthenticated_requests(
        self,
        client: TestClient,
    ):
        """
        Dynamically iterates through every route registered in FastAPI.
        Asserts that any route not in PUBLIC_PATHS returns HTTP 401 Unauthorized when unauthenticated.
        """
        unprotected_routes = []

        for route in app.routes:
            if not isinstance(route, APIRoute):
                continue

            path = route.path
            for method in route.methods:
                if method in ("HEAD", "OPTIONS"):
                    continue

                if path in self.PUBLIC_PATHS and method in self.PUBLIC_PATHS[path]:
                    continue

                # Prepare test path by replacing path params like {match_id} with dummy UUID
                test_path = path.replace("{match_id}", str(uuid.uuid4()))
                test_path = test_path.replace("{user_id}", str(uuid.uuid4()))
                test_path = test_path.replace("{retailer_id}", str(uuid.uuid4()))

                # Ensure client has no cookies or auth headers
                client.cookies.clear()
                client.headers.clear()

                if method == "GET":
                    resp = client.get(test_path)
                elif method == "POST":
                    resp = client.post(test_path, data={})
                elif method == "DELETE":
                    resp = client.delete(test_path)
                else:
                    continue

                if resp.status_code != 401:
                    unprotected_routes.append(f"{method} {path} -> HTTP {resp.status_code}")

        assert len(unprotected_routes) == 0, f"Unauthenticated routes failed to return 401: {unprotected_routes}"


# ---------------------------------------------------------------------------
# 4. CSRF Synchronizer Token Protection
# ---------------------------------------------------------------------------
class TestCsrfProtection:
    def test_state_changing_post_requires_csrf_token(
        self,
        client: TestClient,
        db_session: Session,
        tenant_a: Tenant,
        owner_user_a: User,
    ):
        """POST without CSRF token returns HTTP 403 Forbidden."""
        session_rec, raw_token = create_user_session(db_session, user=owner_user_a)
        db_session.commit()

        client.cookies.set(SESSION_COOKIE_NAME, raw_token)
        # Clear headers to ensure no CSRF token present
        client.headers.pop("x-csrf-token", None)

        # Missing CSRF
        resp = client.post("/sales", data={"invoice_number": "INV-CSRF-1"})
        assert resp.status_code == 403
        assert "Invalid or missing CSRF token" in resp.json()["detail"]

        # Invalid CSRF
        client.headers["x-csrf-token"] = "invalid_csrf_token_hash"
        resp = client.post("/sales", data={"invoice_number": "INV-CSRF-1"})
        assert resp.status_code == 403
        assert "Invalid or missing CSRF token" in resp.json()["detail"]

        # Valid CSRF header
        valid_csrf = generate_csrf_token(session_rec.id, settings.secret_key)
        client.headers["x-csrf-token"] = valid_csrf
        # Now CSRF passes (route proceeds to parameter validation / logic)
        resp = client.post("/sales", data={})
        assert resp.status_code != 403


# ---------------------------------------------------------------------------
# 5. Session Expiry, Logout, and Token Reuse
# ---------------------------------------------------------------------------
class TestSessionLifecycle:
    def test_logout_revokes_token(
        self,
        client: TestClient,
        db_session: Session,
        tenant_a: Tenant,
        owner_user_a: User,
    ):
        """Logging out deletes the session from PostgreSQL and subsequent calls fail."""
        raw_token, csrf_token = authenticate_client(client, db_session, owner_user_a)
        db_session.commit()

        # Dashboard works before logout
        assert client.get("/").status_code == 200

        # Perform logout
        resp = client.post("/logout", follow_redirects=False)
        assert resp.status_code == 303
        assert "/login" in resp.headers["location"]

        # Subsequent call with previous token fails with 401
        client.cookies.set(SESSION_COOKIE_NAME, raw_token)
        resp_after = client.get("/")
        assert resp_after.status_code == 401

    def test_logout_all_revokes_all_user_sessions(
        self,
        client: TestClient,
        db_session: Session,
        tenant_a: Tenant,
        owner_user_a: User,
    ):
        """Logout-all terminates all sessions across all devices for that user."""
        _, tok1 = create_user_session(db_session, user=owner_user_a)
        session_rec2, tok2 = create_user_session(db_session, user=owner_user_a)
        csrf2 = generate_csrf_token(session_rec2.id, settings.secret_key)
        db_session.commit()

        client.cookies.set(SESSION_COOKIE_NAME, tok2)
        client.headers["x-csrf-token"] = csrf2

        resp = client.post("/logout-all", follow_redirects=False)
        assert resp.status_code == 303

        # Both tokens must now be revoked
        client.cookies.set(SESSION_COOKIE_NAME, tok1)
        assert client.get("/").status_code == 401

        client.cookies.set(SESSION_COOKIE_NAME, tok2)
        assert client.get("/").status_code == 401

    def test_idle_timeout_expiration(
        self,
        db_session: Session,
        tenant_a: Tenant,
        owner_user_a: User,
    ):
        """Session inactive longer than idle timeout is expired and removed."""
        session_rec, raw_token = create_user_session(db_session, user=owner_user_a)
        now = datetime.now(timezone.utc)
        # Move last_activity_at back by 65 minutes (default timeout: 60)
        session_rec.last_activity_at = now - timedelta(minutes=65)
        db_session.flush()

        with pytest.raises(Exception) as exc:
            validate_session_token(db_session, raw_token=raw_token, idle_timeout_minutes=60)
        assert "timed out due to inactivity" in str(exc.value)

    def test_absolute_lifetime_expiration(
        self,
        db_session: Session,
        tenant_a: Tenant,
        owner_user_a: User,
    ):
        """Session exceeding absolute lifetime is expired and removed."""
        session_rec, raw_token = create_user_session(db_session, user=owner_user_a, lifetime_hours=12)
        now = datetime.now(timezone.utc)
        # Move expires_at back into the past
        session_rec.expires_at = now - timedelta(minutes=5)
        db_session.flush()

        with pytest.raises(Exception) as exc:
            validate_session_token(db_session, raw_token=raw_token)
        assert "Session has expired" in str(exc.value)


# ---------------------------------------------------------------------------
# 6. Persistent Rate Limiting & Account Lockout in PostgreSQL
# ---------------------------------------------------------------------------
class TestPersistentRateLimitingAndLockout:
    def test_consecutive_failed_logins_trigger_account_lockout(
        self,
        client: TestClient,
        db_session: Session,
        tenant_a: Tenant,
        owner_user_a: User,
    ):
        """5 failed login attempts lock account for 15 minutes; persists across sessions."""
        owner_user_a.password_hash = hash_password("CorrectPassword123!")
        owner_user_a.failed_login_attempts = 0
        owner_user_a.locked_until = None
        db_session.commit()

        # Submit 5 failed attempts
        for _ in range(5):
            resp = client.post(
                "/login",
                data={"identifier": owner_user_a.email, "password": "WrongPassword999!"},
            )
            assert resp.status_code == 401
            assert "Invalid identifier or password" in resp.text

        db_session.expire_all()
        refreshed_user = db_session.get(User, owner_user_a.id)
        assert refreshed_user.failed_login_attempts >= 5
        assert refreshed_user.locked_until is not None
        assert refreshed_user.locked_until > datetime.now(timezone.utc)

        # 6th attempt with CORRECT password still rejected due to lockout (identical 401 per SEC-12)
        resp_locked = client.post(
            "/login",
            data={"identifier": owner_user_a.email, "password": "CorrectPassword123!"},
        )
        assert resp_locked.status_code == 401
        assert "Invalid identifier or password" in resp_locked.text

    def test_rate_limit_bucket_stored_in_postgres(
        self,
        db_session: Session,
    ):
        """Rate limit counters persist in PostgreSQL rate_limit_buckets table."""
        key = f"test:ip:{uuid.uuid4()}"
        check_persistent_rate_limit(db_session, key=key, max_requests=2, window_seconds=60)
        check_persistent_rate_limit(db_session, key=key, max_requests=2, window_seconds=60)
        db_session.commit()

        bucket = db_session.get(RateLimitBucket, key)
        assert bucket is not None
        assert bucket.attempts == 2

        # 3rd request should raise RateLimitExceededError
        with pytest.raises(Exception):
            check_persistent_rate_limit(db_session, key=key, max_requests=2, window_seconds=60)


# ---------------------------------------------------------------------------
# 7. Cross-Tenant Data Isolation Under Authenticated Sessions
# ---------------------------------------------------------------------------
class TestCrossTenantDataIsolation:
    def test_tenant_a_cannot_access_tenant_b_resources(
        self,
        client: TestClient,
        db_session: Session,
        tenant_a: Tenant,
        tenant_b: Tenant,
        owner_user_a: User,
        owner_user_b: User,
    ):
        """User in Tenant A guessing UUIDs of Tenant B receives HTTP 404."""
        now = datetime.now(timezone.utc)
        ret_b = Retailer(id=uuid.uuid4(), tenant_id=tenant_b.id, name="Retailer B", created_at=now)
        inv_b = Invoice(
            id=uuid.uuid4(),
            tenant_id=tenant_b.id,
            retailer_id=ret_b.id,
            invoice_number="INV-B-ISOLATION",
            amount_santim=200000,
            issued_date=now.date(),
            due_date=now.date() + timedelta(days=30),
            created_at=now,
            created_by=owner_user_b.id,
        )
        batch_b = ImportBatch(
            id=uuid.uuid4(),
            tenant_id=tenant_b.id,
            source_type="bank_statement",
            evidence_level=EvidenceLevel.L2.value,
            imported_by=owner_user_b.id,
            imported_at=now,
        )
        pl_b = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=tenant_b.id,
            import_batch_id=batch_b.id,
            value_date=now.date(),
            amount_santim=200000,
            evidence_level=EvidenceLevel.L2.value,
            state="proposed",
            created_at=now,
        )
        pm_b = PaymentMatch(
            id=uuid.uuid4(),
            tenant_id=tenant_b.id,
            invoice_id=inv_b.id,
            payment_line_id=pl_b.id,
            matched_santim=200000,
            tier="tier_a",
            state="proposed",
            created_at=now,
        )
        db_session.add_all([ret_b, inv_b, batch_b, pl_b, pm_b])
        db_session.commit()

        authenticate_client(client, db_session, owner_user_a)

        # 1. Attempt to approve Tenant B match
        resp = client.post(f"/matches/{pm_b.id}/approve")
        assert resp.status_code == 404, "Cross-tenant match must return 404"

        # 2. Attempt to reset Tenant B owner's password
        resp = client.post(f"/settings/users/{owner_user_b.id}/reset-password", data={"new_password": "HackedPassword123!"})
        assert resp.status_code == 404, "Cross-tenant user must return 404"

        # 3. Attempt to deactivate Tenant B owner
        resp = client.post(f"/settings/users/{owner_user_b.id}/deactivate")
        assert resp.status_code == 404, "Cross-tenant user must return 404"


# ---------------------------------------------------------------------------
# 8. Deactivated User Immediate Revocation
# ---------------------------------------------------------------------------
class TestDeactivatedUserRevocation:
    def test_deactivating_user_immediately_invalidates_active_sessions(
        self,
        client: TestClient,
        db_session: Session,
        tenant_a: Tenant,
        owner_user_a: User,
        cashier_user_a: User,
    ):
        """When an owner deactivates a cashier, cashier's session stops working immediately."""
        session_rec, cashier_raw_token = create_user_session(db_session, user=cashier_user_a)
        db_session.commit()

        # Cashier session initially works
        client.cookies.set(SESSION_COOKIE_NAME, cashier_raw_token)
        assert client.get("/").status_code == 200

        # Owner deactivates the cashier
        authenticate_client(client, db_session, owner_user_a)
        resp = client.post(f"/settings/users/{cashier_user_a.id}/deactivate", follow_redirects=False)
        assert resp.status_code == 303

        # Verify DB state
        db_session.expire_all()
        refreshed_cashier = db_session.get(User, cashier_user_a.id)
        assert refreshed_cashier.is_active is False

        # Cashier's active session is terminated
        client.cookies.set(SESSION_COOKIE_NAME, cashier_raw_token)
        resp_after = client.get("/")
        assert resp_after.status_code == 401
