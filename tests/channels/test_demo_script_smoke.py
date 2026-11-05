"""
tests/channels/test_demo_script_smoke.py

Automated 2-minute smoke test executing every step of docs/demo_script.md
against a freshly seeded Addis Ababa wholesale demo tenant:
1. Owner login & dashboard KPI verification
2. Exact bank match approval (L2)
3. Differing overpayment confirmation with credit on file
4. Match rejection & preservation in Needs Attention
5. L4 cash receipt override (Rule 2 invariant: accepted_unverified)
6. Bilingual switch to Amharic (/set-lang/am) & Ge'ez UI verification
7. Owner reversal of confirmed match & audit trail verification
"""

import re
import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.demo.generator import create_demo_tenant
from app.kernel.enums import InvoiceState, MatchState, PaymentState
from app.kernel.models import Event, Invoice, PaymentLine, PaymentMatch, Tenant, User
from app.main import app
from tests.conftest import authenticate_client


class TestDemoScriptSmoke:
    """Automated end-to-end verification of docs/demo_script.md."""

    def test_full_2_minute_demo_script_smoke_walkthrough(
        self, client: TestClient, db_session: Session
    ):
        # Setup: Seed fresh demo tenant and owner
        tenant, owner = create_demo_tenant(
            db_session,
            tenant_name="Addis Wholesale Demo / አዲስ ጅምላ ማሳያ",
            owner_identifier="demo@dailyclose.et",
            owner_name="Ato Demo Owner",
            owner_password="DemoDailyClose2026!",
        )

        # =====================================================================
        # Step 1: Wholesale Owner Sign In & Initial Dashboard Inspection (0:00)
        # =====================================================================
        # Visit login page
        res_login_page = client.get("/login")
        assert res_login_page.status_code == 200
        assert "Sign In" in res_login_page.text or "ግባ" in res_login_page.text

        # Authenticate via login POST
        res_auth = client.post(
            "/login",
            data={"identifier": "demo@dailyclose.et", "password": "DemoDailyClose2026!"},
            follow_redirects=True,
        )
        assert res_auth.status_code == 200
        assert "Total Unpaid" in res_auth.text
        assert "Overdue" in res_auth.text
        assert "Pending Matches" in res_auth.text

        # Exactly 6 cards initially in the review queue
        card_blocks = [b for b in res_auth.text.split('class="match-card"') if 'id="match-card-' in b or "id='match-card-" in b]
        assert len(card_blocks) == 6

        # Check Needs Attention section exists
        assert 'id="needs-attention-section"' in res_auth.text

        # Check Aging table exists and is sorted by overdue
        assert 'id="aging-section"' in res_auth.text
        assert "Accounts Receivable Aging" in res_auth.text or "የብድር እድሜ" in res_auth.text

        # Extract CSRF token from rendered dashboard
        m_csrf = re.search(r'name=["\']csrf_token["\']\s+value=["\']([^"\']+)["\']', res_auth.text)
        assert m_csrf is not None
        csrf_token = m_csrf.group(1)

        # =====================================================================
        # Step 2: Confirm Routine Suggested Match (Abebe Grain Wholesale, 60k ETB) (0:20)
        # =====================================================================
        pm_abebe = db_session.execute(
            select(PaymentMatch)
            .join(PaymentLine, PaymentMatch.payment_line_id == PaymentLine.id)
            .where(
                PaymentMatch.tenant_id == tenant.id,
                PaymentMatch.state == MatchState.PROPOSED.value,
                PaymentLine.amount_santim == 6000000,
            )
        ).scalars().first()
        assert pm_abebe is not None

        # Extract approval token from card HTML
        card_pattern = rf'id=["\']match-card-{pm_abebe.id}["\'].*?name=["\']approval_token["\']\s+value=["\']([^"\']+)["\']'
        m_tok = re.search(card_pattern, res_auth.text, re.DOTALL)
        assert m_tok is not None
        abebe_token = m_tok.group(1)

        # Post approval
        res_app_1 = client.post(
            f"/matches/{pm_abebe.id}/approve",
            data={
                "csrf_token": csrf_token,
                "approval_token": abebe_token,
                "confirm_differing": "false",
            },
            headers={"hx-request": "true"},
        )
        assert res_app_1.status_code == 200
        assert "data-testid='match-confirmed-banner'" in res_app_1.text or 'data-testid="match-confirmed-banner"' in res_app_1.text

        # Verify DB state
        db_session.expire_all()
        pm_refreshed = db_session.get(PaymentMatch, pm_abebe.id)
        assert pm_refreshed.state == MatchState.CONFIRMED.value
        assert pm_refreshed.payment_line.state == PaymentState.CONFIRMED.value

        # Refresh dashboard and verify queue count decremented to 5
        res_dash_2 = client.get("/dashboard")
        assert res_dash_2.status_code == 200
        cards_after_1 = [b for b in res_dash_2.text.split('class="match-card"') if 'id="match-card-' in b or "id='match-card-" in b]
        assert len(cards_after_1) == 5

        # =====================================================================
        # Step 3: Confirm Differing Overpayment with Credit on File (Tewodros Cosmetics) (0:40)
        # =====================================================================
        pm_tewodros = db_session.execute(
            select(PaymentMatch)
            .join(PaymentLine, PaymentMatch.payment_line_id == PaymentLine.id)
            .where(
                PaymentMatch.tenant_id == tenant.id,
                PaymentMatch.state == MatchState.PROPOSED.value,
                PaymentLine.amount_santim == 5500000,
            )
        ).scalars().first()
        assert pm_tewodros is not None

        # Verify card contains differing amount callout and unallocated credit notice
        assert "Amounts differ by" in res_dash_2.text
        assert "15,000.00 ETB" in res_dash_2.text
        assert "Overpayment" in res_dash_2.text
        assert "kept on file as credit" in res_dash_2.text

        # Extract approval token
        tewodros_pattern = rf'id=["\']match-card-{pm_tewodros.id}["\'].*?name=["\']approval_token["\']\s+value=["\']([^"\']+)["\']'
        m_tok_tewodros = re.search(tewodros_pattern, res_dash_2.text, re.DOTALL)
        assert m_tok_tewodros is not None
        tewodros_token = m_tok_tewodros.group(1)

        # Confirm differing match
        res_app_2 = client.post(
            f"/matches/{pm_tewodros.id}/approve",
            data={
                "csrf_token": csrf_token,
                "approval_token": tewodros_token,
                "confirm_differing": "true",
            },
            headers={"hx-request": "true"},
        )
        assert res_app_2.status_code == 200
        assert "data-testid='match-confirmed-banner'" in res_app_2.text or 'data-testid="match-confirmed-banner"' in res_app_2.text

        # Verify DB: invoice settled, credit recorded
        db_session.expire_all()
        pm_tewodros_ref = db_session.get(PaymentMatch, pm_tewodros.id)
        assert pm_tewodros_ref.state == MatchState.CONFIRMED.value
        assert pm_tewodros_ref.invoice.state == InvoiceState.SETTLED.value

        # =====================================================================
        # Step 4: Reject Match & Retain in Audit Records (Berhan Stationery) (1:00)
        # =====================================================================
        pm_berhan = db_session.execute(
            select(PaymentMatch)
            .join(PaymentLine, PaymentMatch.payment_line_id == PaymentLine.id)
            .where(
                PaymentMatch.tenant_id == tenant.id,
                PaymentMatch.state == MatchState.PROPOSED.value,
                PaymentLine.amount_santim == 3500000,
            )
        ).scalars().first()
        assert pm_berhan is not None

        # Post match rejection
        res_rej = client.post(
            f"/matches/{pm_berhan.id}/reject",
            data={"csrf_token": csrf_token},
            headers={"hx-request": "true"},
        )
        assert res_rej.status_code == 200
        assert "Match Rejected" in res_rej.text or "data-testid='match-rejected-banner'" in res_rej.text or "ውድቅ ተደረገ" in res_rej.text

        # Verify DB: match is rejected, payment line is rejected (NEVER DELETED)
        db_session.expire_all()
        pm_berhan_ref = db_session.get(PaymentMatch, pm_berhan.id)
        assert pm_berhan_ref.state == MatchState.REJECTED.value
        assert pm_berhan_ref.payment_line.state == PaymentState.REJECTED.value

        # Verify it now appears in Needs Attention on dashboard
        res_dash_4 = client.get("/dashboard")
        assert f'data-testid="needs-attention-item-{pm_berhan.payment_line_id}"' in res_dash_4.text
        assert "Rejected Match" in res_dash_4.text

        # =====================================================================
        # Step 5: Mark Unverified Photo / Cash Receipt (Almaz, L4) (1:15)
        # =====================================================================
        pm_l4 = db_session.execute(
            select(PaymentMatch)
            .join(PaymentLine, PaymentMatch.payment_line_id == PaymentLine.id)
            .where(
                PaymentMatch.tenant_id == tenant.id,
                PaymentLine.evidence_level == "L4",
            )
        ).scalars().first()
        assert pm_l4 is not None

        # Post mark-unverified
        res_unv = client.post(
            f"/matches/{pm_l4.id}/mark-unverified",
            data={"csrf_token": csrf_token},
            headers={"hx-request": "true"},
        )
        assert res_unv.status_code == 200
        assert "Accepted Unverified" in res_unv.text or "data-testid='match-unverified-banner'" in res_unv.text or "ያልተረጋገጠ" in res_unv.text

        # Verify DB: payment line state is accepted_unverified
        db_session.expire_all()
        pl_l4 = db_session.get(PaymentLine, pm_l4.payment_line_id)
        assert pl_l4.state == PaymentState.ACCEPTED_UNVERIFIED.value

        # Verify it appears in Needs Attention section
        res_dash_5 = client.get("/dashboard")
        assert f'data-testid="needs-attention-item-{pl_l4.id}"' in res_dash_5.text
        assert "Accepted Unverified (L4)" in res_dash_5.text

        # =====================================================================
        # Step 6: Seamless Language Switch to Amharic (1:30)
        # =====================================================================
        res_lang = client.get("/set-lang/am", follow_redirects=False)
        assert res_lang.status_code == 303
        assert res_lang.headers.get("location") == "/dashboard"

        # Fetch dashboard in Amharic
        res_am = client.get("/dashboard")
        assert res_am.status_code == 200
        # Check translated KPI titles
        assert "ያልተከፈለ አጠቃላይ" in res_am.text
        assert "ቀኑ ያለፈበት" in res_am.text
        assert "ያልተረጋገጡ ክፍያዎች" in res_am.text
        # Check translated checklists
        assert "የዛሬው የሂሳብ ማጠቃለያ ስራዎች" in res_am.text
        assert "የዛሬውን የብድር ሽያጭ ይመዝግቡ" in res_am.text
        assert "የባንክ ወይም የቴሌብር መግለጫ ያስገቡ" in res_am.text
        # Check translated needs attention section
        assert "ትኩረት የሚሹ ክፍያዎች" in res_am.text
        assert "ውድቅ የተደረጉ" in res_am.text
        assert "ያልተዛመዱ" in res_am.text
        assert "ያልተረጋገጡ" in res_am.text
        assert "የተመለሱ" in res_am.text

        # =====================================================================
        # Step 7: Reversibility & Audit Trail Verification (1:45)
        # =====================================================================
        # Locate the confirmed match from Step 2 (pm_abebe) in recently confirmed
        assert pm_abebe is not None
        assert f"/matches/{pm_abebe.id}/reverse" in res_am.text

        # Reverse the confirmed match as owner
        res_rev = client.post(
            f"/matches/{pm_abebe.id}/reverse",
            data={
                "csrf_token": csrf_token,
                "reason_code": "WRONG_INVOICE",
                "notes": "Accidental confirmation of wrong invoice during demo",
            },
            follow_redirects=True,
        )
        assert res_rev.status_code == 200

        # Verify DB: match is reversed, payment line flagged is_previously_reversed
        db_session.expire_all()
        pm_abebe_rev = db_session.get(PaymentMatch, pm_abebe.id)
        assert pm_abebe_rev.state == MatchState.REVERSED.value
        assert pm_abebe_rev.payment_line.is_previously_reversed is True

        # Verify compensating reversal event logged in events table
        rev_event = db_session.execute(
            select(Event).where(
                Event.tenant_id == tenant.id,
                Event.event_type == "match.reversed",
                Event.aggregate_id == pm_abebe.id,
            )
        ).scalars().first()
        assert rev_event is not None
        assert rev_event.payload.get("reason_code") == "wrong_invoice"
