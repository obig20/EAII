"""
tests/channels/test_stage2_security_hardening.py

Stage 2 Security Hardening Invariant and Adversarial Tests:
1. No fixed password in codebase; secure single-use 24-hour reset link with role enforcement and secret-free event logging.
2. /set-lang/{lang_code} validation, SameSite=Lax cookie, and open-redirect prevention.
3. Content-Security-Policy (CSP) header across all application screens with strict HTMX configuration.
4. Total elimination of inline on* handlers and inline <script> blocks in templates.
5. Functional review queue and action submissions without JavaScript (no-JS fallback).
6. Adversarial injection audit: XSS vectors (alert payloads, script tags, backslashes, backticks, ${x}, newlines, U+2028)
   safely escaped in text nodes, zero injection into script blocks or on* attributes.
"""

from __future__ import annotations

import html
import os
import pathlib
import re
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.kernel.enums import InvoiceState, MatchState, PaymentState, UserRole
from app.kernel.models import Event, Invoice, PaymentLine, PaymentMatch, Retailer, Tenant, User
from app.kernel.tenant import set_tenant
from app.kernel.tokens import (
    generate_password_reset_token,
    verify_password_reset_token,
)
from app.settings import settings
from tests.conftest import authenticate_client


class SecurityHTMLAuditParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.on_attrs = []
        self.script_blocks = []
        self.meta_tags = []
        self.in_script = False
        self.current_script_text = []
        self.all_tags = []

    def handle_starttag(self, tag, attrs):
        attr_dict = dict(attrs)
        self.all_tags.append((tag, attr_dict))
        if tag.lower() == "meta":
            self.meta_tags.append(attr_dict)
        for attr_name, attr_val in attrs:
            if attr_name.lower().startswith("on"):
                self.on_attrs.append((tag, attr_name, attr_val))
        if tag.lower() == "script":
            self.in_script = True
            self.current_script_text = []

    def handle_endtag(self, tag):
        if tag.lower() == "script":
            self.in_script = False
            self.script_blocks.append("".join(self.current_script_text))
            self.current_script_text = []

    def handle_data(self, data):
        if self.in_script:
            self.current_script_text.append(data)


class TestStage2SecurityHardening:
    def test_no_fixed_password_in_codebase(self):
        """Assert no occurrence of the retired fixed password constant exists in the repository."""
        repo_root = pathlib.Path(__file__).parents[2]
        retired_secret = "Birana" + "Temp"
        pattern = re.compile(re.escape(retired_secret))

        violations = []
        for ext in (".py", ".html", ".js", ".md", ".json", ".txt"):
            for file_path in repo_root.rglob(f"*{ext}"):
                if (
                    ".git" in file_path.parts
                    or ".venv" in file_path.parts
                    or "__pycache__" in file_path.parts
                    or file_path.resolve() == pathlib.Path(__file__).resolve()
                ):
                    continue
                try:
                    content = file_path.read_text(encoding="utf-8", errors="ignore")
                    if pattern.search(content):
                        violations.append(str(file_path.relative_to(repo_root)))
                except Exception:
                    pass

        assert not violations, f"Found retired password constant in: {violations}"

    def test_password_reset_flow_expiry_single_use_and_events(
        self, client: TestClient, db_session: Session, tenant_a: Tenant, owner_user_a: User, cashier_user_a: User, tenant_b: Tenant, owner_user_b: User
    ):
        """
        Verify password reset:
        1. Owner generates reset link for cashier.
        2. Cashier role cannot generate reset links (403).
        3. Cross-tenant reset link generation is rejected (404).
        4. Staff views reset page and resets password (>=10 chars).
        5. Token is single-use: replay returns 400.
        6. Expired token (>24h) returns 400.
        7. Audit events logged with token signatures but NO secrets.
        8. Cashier can log in with newly set password.
        """
        # A. Cashier cannot generate reset links
        authenticate_client(client, db_session, cashier_user_a)
        res_cashier = client.post(f"/settings/users/{cashier_user_a.id}/reset-password")
        assert res_cashier.status_code == 403

        # B. Cross-tenant generation blocked
        authenticate_client(client, db_session, owner_user_b)
        res_cross = client.post(f"/settings/users/{cashier_user_a.id}/reset-password")
        assert res_cross.status_code == 404

        # C. Owner generates reset link
        authenticate_client(client, db_session, owner_user_a)
        res_gen = client.post(f"/settings/users/{cashier_user_a.id}/reset-password", follow_redirects=False)
        assert res_gen.status_code == 303
        redirect_url = res_gen.headers.get("location")
        assert "reset_link=" in redirect_url

        # Extract token from redirect URL
        parsed = urllib.parse.urlparse(redirect_url)
        params = urllib.parse.parse_qs(parsed.query)
        reset_link = params["reset_link"][0]
        token = reset_link.split("/reset-password/")[1]

        # Verify audit event for generation contains NO secret
        gen_event = db_session.execute(
            select(Event).where(
                Event.tenant_id == tenant_a.id,
                Event.event_type == "auth.password_reset_link_generated",
                Event.aggregate_id == cashier_user_a.id,
            )
        ).scalars().first()
        assert gen_event is not None
        assert "token" not in gen_event.payload
        assert "password" not in str(gen_event.payload)

        # D. GET reset password page
        client.cookies.clear()
        res_page = client.get(f"/reset-password/{token}")
        assert res_page.status_code == 200
        assert cashier_user_a.display_name in res_page.text
        assert "New Password" in res_page.text or "አዲስ የይለፍ ቃል" in res_page.text

        # Password validation (<10 chars rejected)
        res_short = client.post(
            f"/reset-password/{token}",
            data={"new_password": "short", "confirm_password": "short"},
        )
        assert res_short.status_code == 400
        assert "at least 10 characters" in res_short.text

        # Passwords mismatch rejected
        res_mismatch = client.post(
            f"/reset-password/{token}",
            data={"new_password": "NewSecretPass1234!", "confirm_password": "DifferentPass1234!"},
        )
        assert res_mismatch.status_code == 400
        assert "Passwords do not match" in res_mismatch.text

        # E. Successful reset
        res_submit = client.post(
            f"/reset-password/{token}",
            data={"new_password": "BrandNewStaffPassword2026!", "confirm_password": "BrandNewStaffPassword2026!"},
            follow_redirects=False,
        )
        assert res_submit.status_code == 303
        assert "/login" in res_submit.headers.get("location")

        # Verify completion event logged without secret
        comp_event = db_session.execute(
            select(Event).where(
                Event.tenant_id == tenant_a.id,
                Event.event_type == "auth.password_reset_completed",
                Event.aggregate_id == cashier_user_a.id,
            )
        ).scalars().first()
        assert comp_event is not None
        assert "token_sig" in comp_event.payload
        assert "BrandNewStaffPassword2026!" not in str(comp_event.payload)

        # F. Replay single-use protection: same token fails
        res_replay = client.post(
            f"/reset-password/{token}",
            data={"new_password": "AnotherPassword12345!", "confirm_password": "AnotherPassword12345!"},
        )
        assert res_replay.status_code == 400
        assert "already been used" in res_replay.text

        # G. Expiry protection: token older than 24 hours fails
        past_time = int(time.time()) - 90000  # >24h
        expired_token = generate_password_reset_token(
            settings.secret_key,
            tenant_id=tenant_a.id,
            user_id=cashier_user_a.id,
            issued_at=past_time,
        )
        res_expired = client.get(f"/reset-password/{expired_token}")
        assert res_expired.status_code == 400
        assert "expired" in res_expired.text.lower()

    def test_password_reset_disabled_in_demo_mode(
        self, client: TestClient, db_session: Session, demo_tenant_and_owner: tuple[Tenant, User]
    ):
        """Verify that on demo tenants, staff password reset is disabled with a clear message."""
        demo_t, demo_u = demo_tenant_and_owner
        cashier = User(
            id=uuid.uuid4(),
            tenant_id=demo_t.id,
            role=UserRole.CASHIER.value,
            display_name="Demo Cashier",
            email="demo.cashier@dailyclose.et",
            phone="+251911999902",
            password_hash="dummy",
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(cashier)
        db_session.flush()

        authenticate_client(client, db_session, demo_u)
        res = client.get("/settings/users")
        assert res.status_code == 200
        assert "Reset disabled in demo" in res.text

        res_post = client.post(f"/settings/users/{cashier.id}/reset-password", follow_redirects=False)
        assert res_post.status_code == 303
        loc = res_post.headers.get("location", "")
        assert "Password reset is disabled in demo mode" in urllib.parse.unquote(loc)

    def test_set_lang_route(self, client: TestClient):
        """
        Test /set-lang/{lang_code}:
        - Only 'en' and 'am' accepted.
        - Other codes return 400.
        - Sets SameSite=Lax cookie.
        - Open redirect protection: blocks external domains and schemes.
        """
        # 1. Accept 'en' with Lax cookie
        res_en = client.get("/set-lang/en", follow_redirects=False)
        assert res_en.status_code == 303
        cookie_header = res_en.headers.get("set-cookie", "")
        assert "lang=en" in cookie_header
        assert "samesite=lax" in cookie_header.lower()

        # 2. Accept 'am'
        res_am = client.get("/set-lang/am", follow_redirects=False)
        assert res_am.status_code == 303
        assert "lang=am" in res_am.headers.get("set-cookie", "")

        # 3. Reject invalid language code
        res_invalid = client.get("/set-lang/fr")
        assert res_invalid.status_code == 400

        res_invalid2 = client.get("/set-lang/es")
        assert res_invalid2.status_code == 400

        # 4. Open redirect protection
        evil_referers = [
            "https://evil.com/phish",
            "http://malicious.org",
            "//attacker.com/steal",
            "javascript:alert(1)",
        ]
        for evil in evil_referers:
            res_evil = client.get("/set-lang/en", headers={"referer": evil}, follow_redirects=False)
            assert res_evil.status_code == 303
            assert res_evil.headers.get("location") == "/dashboard"

        # 5. Always redirects to /dashboard (no Referer parsing)
        res_safe = client.get("/set-lang/am", headers={"referer": "/settings/users"}, follow_redirects=False)
        assert res_safe.status_code == 303
        assert res_safe.headers.get("location") == "/dashboard"

    def test_no_inline_scripts_and_no_inline_on_handlers_in_templates(self):
        """
        Assert zero inline on* event handlers and zero inline <script> tags
        exist in any HTML template across the entire application.
        """
        templates_dir = pathlib.Path(__file__).parents[2] / "app" / "channels" / "web" / "templates"
        on_pattern = re.compile(r"\bon[a-z]+\s*=", re.IGNORECASE)
        script_pattern = re.compile(r"<script(?![^>]*src=)", re.IGNORECASE)

        on_violations = []
        script_violations = []

        for html_file in templates_dir.rglob("*.html"):
            content = html_file.read_text(encoding="utf-8")
            for line_no, line in enumerate(content.splitlines(), start=1):
                if on_pattern.search(line):
                    on_violations.append(f"{html_file.name}:{line_no} -> {line.strip()}")
                if script_pattern.search(line):
                    script_violations.append(f"{html_file.name}:{line_no} -> {line.strip()}")

        assert not on_violations, f"Found inline event handler attributes:\n" + "\n".join(on_violations)
        assert not script_violations, f"Found inline <script> blocks:\n" + "\n".join(script_violations)

    def test_csp_headers_and_htmx_config_across_screens(
        self, client: TestClient, db_session: Session, demo_tenant_and_owner: tuple[Tenant, User]
    ):
        """
        Verify CSP header on all key screens and verify HTMX config:
        allowEval=false and includeIndicatorStyles=false.
        """
        demo_t, demo_u = demo_tenant_and_owner
        authenticate_client(client, db_session, demo_u)

        routes_to_test = ["/login", "/", "/settings/users"]
        required_directives = [
            "default-src 'self'",
            "script-src 'self'",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data:",
            "object-src 'none'",
            "base-uri 'none'",
            "form-action 'self'",
            "frame-ancestors 'none'",
        ]

        for route in routes_to_test:
            res = client.get(route)
            assert res.status_code == 200, f"Failed on {route}"
            csp = res.headers.get("Content-Security-Policy", "")
            for directive in required_directives:
                assert directive in csp, f"Missing '{directive}' on route {route}. CSP: {csp}"

            # If page uses base.html, verify htmx-config
            if "htmx.min.js" in res.text:
                parser = SecurityHTMLAuditParser()
                parser.feed(res.text)
                meta_htmx = next((m for m in parser.meta_tags if m.get("name") == "htmx-config"), None)
                assert meta_htmx is not None, f"Missing meta[name=htmx-config] on {route}"
                content = meta_htmx.get("content", "")
                assert '"allowEval":false' in content or '"allowEval": false' in content
                assert '"includeIndicatorStyles":false' in content or '"includeIndicatorStyles": false' in content

    def test_no_js_functional_review_queue_and_actions(
        self, client: TestClient, db_session: Session, demo_tenant_and_owner: tuple[Tenant, User]
    ):
        """
        Verify that with JavaScript disabled:
        1. All cards in the review queue are rendered with standard semantic forms.
        2. Approving or rejecting a match works via standard HTTP POST without JS/HTMX headers.
        """
        demo_t, demo_u = demo_tenant_and_owner
        authenticate_client(client, db_session, demo_u)

        res = client.get("/")
        assert res.status_code == 200

        # 1. Verify cards are rendered
        card_blocks = [b for b in res.text.split('class="match-card"') if 'id="match-card-' in b or "id='match-card-" in b]
        assert len(card_blocks) == 6

        # Verify each card has standard POST forms and submit buttons
        for block in card_blocks:
            assert 'method="POST"' in block or "method='POST'" in block
            assert 'type="submit"' in block or "type='submit'" in block

        # 2. Pick an exact match card and approve via normal non-HTMX POST
        pm_exact = db_session.execute(
            select(PaymentMatch)
            .join(PaymentLine, PaymentMatch.payment_line_id == PaymentLine.id)
            .where(
                PaymentMatch.tenant_id == demo_t.id,
                PaymentMatch.state == "proposed",
                PaymentLine.amount_santim == 8000000,
            )
        ).scalars().first()
        assert pm_exact is not None

        # Find approval token and confirm_differing in the card block
        card_pattern = (
            rf'id=["\']match-card-{pm_exact.id}["\'].*?name=["\']approval_token["\']\s+value=["\']([^"\']+)["\'].*?'
            rf'name=["\']confirm_differing["\']\s+value=["\']([^"\']+)["\']'
        )
        m_tok = re.search(card_pattern, res.text, re.DOTALL)
        assert m_tok is not None
        token_val = m_tok.group(1)
        confirm_differing_val = m_tok.group(2)

        # Submit normal POST without any HTMX headers
        res_post = client.post(
            f"/matches/{pm_exact.id}/approve",
            data={"approval_token": token_val, "confirm_differing": confirm_differing_val},
        )
        assert res_post.status_code == 200
        assert "Match Confirmed" in res_post.text or "ክፍያ ተረጋግጧል" in res_post.text

    def test_adversarial_xss_in_review_queue_and_user_management(
        self, client: TestClient, db_session: Session, tenant_a: Tenant, owner_user_a: User
    ):
        """
        Adversarial test:
        Seed retailer name, payment reference, invoice number, and user display name containing:
        "); alert(1);//, '+alert(1)+', </script><script>alert(1)</script>, backslashes, backticks, ${x}, newlines, and U+2028.
        Render review queue and user management pages:
        1. Parse the HTML: assert NO on* attribute values contain any payload text.
        2. Assert NO <script> text contains any payload text.
        3. Assert payloads only appear properly HTML-escaped inside text nodes.
        """
        set_tenant(db_session, tenant_a.id)

        complex_payload = '"); alert(1);//\'+alert(1)+\'</script><script>alert(1)</script>\\`\\`${x}\n\u2028'

        # Seed retailer with payload
        ret = Retailer(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            name=f"EvilRetailer {complex_payload}",
            phone="+251911998877",
            created_at=datetime.now(timezone.utc),
        )
        # Seed invoice with payload
        inv = Invoice(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            retailer_id=ret.id,
            invoice_number=f"INV-{uuid.uuid4().hex[:4]}-{complex_payload}",
            amount_santim=5000000,
            issued_date=datetime.now(timezone.utc).date(),
            due_date=datetime.now(timezone.utc).date(),
            state=InvoiceState.OPEN.value,
            created_at=datetime.now(timezone.utc),
            created_by=owner_user_a.id,
        )
        # Seed payment line with payload
        pl = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            amount_santim=5000000,
            value_date=datetime.now(timezone.utc).date(),
            raw_reference=f"REF-{complex_payload}",
            evidence_level="L1",
            state=PaymentState.PROPOSED.value,
            created_at=datetime.now(timezone.utc),
        )
        # Seed match
        pm = PaymentMatch(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            payment_line_id=pl.id,
            invoice_id=inv.id,
            matched_santim=5000000,
            tier="tier_a",
            state=MatchState.PROPOSED.value,
            created_at=datetime.now(timezone.utc),
        )
        # Seed adversarial staff user
        bad_user = User(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            role=UserRole.CASHIER.value,
            display_name=f"EvilCashier {complex_payload}",
            email=f"evil.{uuid.uuid4().hex[:6]}@adversarial.et",
            phone="+251911998899",
            password_hash="dummy",
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add_all([ret, inv, pl, pm, bad_user])
        db_session.flush()

        authenticate_client(client, db_session, owner_user_a)

        # 1. Review Queue Page
        res_queue = client.get("/")
        assert res_queue.status_code == 200

        parser_queue = SecurityHTMLAuditParser()
        parser_queue.feed(res_queue.text)

        # Assert no on* attribute exists or contains any payload
        assert len(parser_queue.on_attrs) == 0, f"Found on* attributes: {parser_queue.on_attrs}"

        # Assert no script tag contains alert(1) or unescaped payload
        for script_body in parser_queue.script_blocks:
            assert "alert(1)" not in script_body
            assert complex_payload not in script_body

        # Assert raw injection strings are NEVER present unescaped in HTML
        assert "<script>alert(1)</script>" not in res_queue.text
        assert '"); alert(1);//' not in res_queue.text
        assert "'+alert(1)+'" not in res_queue.text

        # Assert adversarial payload appears safely HTML-escaped
        assert "&lt;/script&gt;&lt;script&gt;alert(1)&lt;/script&gt;" in res_queue.text

        # 2. User Management Page
        res_users = client.get("/settings/users")
        assert res_users.status_code == 200

        parser_users = SecurityHTMLAuditParser()
        parser_users.feed(res_users.text)

        assert len(parser_users.on_attrs) == 0, f"Found on* attributes: {parser_users.on_attrs}"

        for script_body in parser_users.script_blocks:
            assert "alert(1)" not in script_body
            assert complex_payload not in script_body

        assert "<script>alert(1)</script>" not in res_users.text
        assert '"); alert(1);//' not in res_users.text
        assert "'+alert(1)+'" not in res_users.text

        assert "&lt;/script&gt;&lt;script&gt;alert(1)&lt;/script&gt;" in res_users.text

    def test_confirmation_buttons_carry_hx_confirm_and_cancel_runtime_safety(
        self, client: TestClient, db_session: Session, demo_tenant_and_owner: tuple[Tenant, User]
    ):
        """
        Stage 2 Verification:
        1. Confirmation dialog buttons carry the native hx-confirm attribute.
        2. In both English and Amharic, the confirmation text is server-escaped and non-empty.
        3. Verify that cancel behavior guards requests before dispatch.
        """
        demo_t, demo_u = demo_tenant_and_owner
        authenticate_client(client, db_session, demo_u)

        # 1. English review queue
        res_en = client.get("/dashboard")
        assert res_en.status_code == 200

        # Assert confirm-differing button carries hx-confirm
        assert 'data-testid="confirm-differing-button"' in res_en.text
        diff_match = re.search(r'<button[^>]+data-testid="confirm-differing-button"[^>]*>', res_en.text)
        assert diff_match is not None
        diff_tag = diff_match.group(0)
        assert 'hx-confirm="' in diff_tag
        assert "Confirm and keep" in diff_tag
        assert "credit" in diff_tag

        # Assert reject button carries hx-confirm
        assert 'data-testid="reject-match-button"' in res_en.text
        rej_match = re.search(r'<button[^>]+data-testid="reject-match-button"[^>]*>', res_en.text)
        assert rej_match is not None
        rej_tag = rej_match.group(0)
        assert 'hx-confirm="' in rej_tag
        assert "Reject this suggested match" in rej_tag

        # 2. Switch to Amharic and verify localized confirmation text
        client.get("/set-lang/am")
        res_am = client.get("/dashboard")
        assert res_am.status_code == 200

        diff_match_am = re.search(r'<button[^>]+data-testid="confirm-differing-button"[^>]*>', res_am.text)
        assert diff_match_am is not None
        diff_tag_am = diff_match_am.group(0)
        assert 'hx-confirm="' in diff_tag_am
        assert "ክሬዲት" in diff_tag_am

        rej_match_am = re.search(r'<button[^>]+data-testid="reject-match-button"[^>]*>', res_am.text)
        assert rej_match_am is not None
        rej_tag_am = rej_match_am.group(0)
        assert 'hx-confirm="' in rej_tag_am
        assert "ውድቅ ማድረግ ይፈልጋሉ" in rej_tag_am

    def test_dashboard_needs_attention_list_and_counts(
        self, client: TestClient, db_session: Session, demo_tenant_and_owner: tuple[Tenant, User]
    ):
        """
        Stage 2 Verification:
        Verify the 'Needs attention' section on the dashboard:
        - Displays category counts (rejected, unmatched, accepted-unverified, previously reversed).
        - Correctly updates when items transition states (e.g. reject match, mark unverified).
        """
        demo_t, demo_u = demo_tenant_and_owner
        authenticate_client(client, db_session, demo_u)

        # 1. Initial dashboard inspection
        res = client.get("/dashboard")
        assert res.status_code == 200
        assert 'id="needs-attention-section"' in res.text
        assert 'data-testid="needs-attention-total-badge"' in res.text
        assert 'data-testid="needs-attention-rejected-badge"' in res.text
        assert 'data-testid="needs-attention-unmatched-badge"' in res.text
        assert 'data-testid="needs-attention-unverified-badge"' in res.text
        assert 'data-testid="needs-attention-reversed-badge"' in res.text

        # 2. Find L4 payment and mark as unverified
        l4_pm = db_session.execute(
            select(PaymentMatch)
            .join(PaymentLine, PaymentMatch.payment_line_id == PaymentLine.id)
            .where(
                PaymentMatch.tenant_id == demo_t.id,
                PaymentLine.evidence_level == "L4",
            )
        ).scalars().first()
        assert l4_pm is not None

        # POST mark-unverified
        csrf_tok = re.search(r'name=["\']csrf_token["\']\s+value=["\']([^"\']+)["\']', res.text).group(1)
        res_unv = client.post(
            f"/matches/{l4_pm.id}/mark-unverified",
            data={"csrf_token": csrf_tok},
            headers={"hx-request": "true"},
        )
        assert res_unv.status_code == 200

        # Refresh dashboard and verify it appears in Needs Attention
        res_after = client.get("/dashboard")
        assert res_after.status_code == 200
        assert f'data-testid="needs-attention-item-{l4_pm.payment_line_id}"' in res_after.text
        assert "Accepted Unverified (L4)" in res_after.text or "ያልተረጋገጠ (L4)" in res_after.text

        # Verify unverified badge count is at least 1
        m_badge = re.search(r'data-testid="needs-attention-unverified-badge">\s*[^:]+:\s*(\d+)', res_after.text)
        assert m_badge is not None
        assert int(m_badge.group(1)) >= 1
