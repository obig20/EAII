"""
tests/matching/test_payer_memory.py

Comprehensive tests for Stage 2: Payer Memory
Validates:
  1. Multi-tenant RLS isolation
  2. Cashier 403 Forbidden on all alias endpoints
  3. Alias creation, retrieval, and soft deletion
  4. Ambiguity handling when a payer is linked to multiple retailers
  5. Amharic script and Latin transliteration name normalization
  6. HTML escaping and XSS injection prevention
  7. Absolute Invariance: Payer memory proposals NEVER produce 'auto_confirmed' or 'confirmed'
  8. End-to-end web workflows (create, list, delete)
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from app.kernel.enums import EvidenceLevel, InvoiceState, MatchTier
from app.kernel.models import Event, Invoice, PayerAlias, PaymentLine, Retailer, Tenant, User
from app.kernel.tenant import set_tenant
from app.matching.payer_memory import (
    create_payer_alias,
    delete_payer_alias,
    evaluate_payer_memory_proposal,
    find_matching_payer_aliases,
    list_payer_aliases,
    normalize_payer_name,
    normalize_payer_reference,
)
from tests.conftest import authenticate_client


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today() -> date:
    return datetime.now(timezone.utc).date()


class TestPayerMemoryUnitAndDomain:
    def test_name_and_ref_normalization(self):
        """Test normalization for Latin and Amharic names, and phone/account refs."""
        # 1. Latin name normalization
        assert normalize_payer_name("Abebe Bekele Shop") == normalize_payer_name("abebe bekele")
        # 2. Amharic name normalization
        am_norm = normalize_payer_name("አበበ በቀለ ሱቅ")
        en_norm = normalize_payer_name("Abebe Bekele")
        assert am_norm == en_norm

        # 3. Phone normalization (Ethiopian national 9-digit format)
        assert normalize_payer_reference("+251 91 122 3344") == "911223344"
        assert normalize_payer_reference("0911223344") == "911223344"

        # 4. Bank account normalization
        assert normalize_payer_reference("1000-1234-5678") == "100012345678"

    def test_payer_alias_creation_and_soft_deletion(
        self,
        db_session: Session,
        tenant_a: Tenant,
        owner_user_a: User,
    ):
        """Owner creates an alias, checks listing, then soft-deletes it."""
        set_tenant(db_session, tenant_a.id)

        retailer = Retailer(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            name="Alpha Merkato Shop",
            phone="+251911000111",
            created_at=_now(),
        )
        db_session.add(retailer)
        db_session.flush()

        alias = create_payer_alias(
            db_session,
            tenant_id=tenant_a.id,
            retailer_id=retailer.id,
            raw_payer_name="Almaz Bekele (Spouse)",
            raw_payer_ref="0911998877",
            user_id=owner_user_a.id,
        )
        db_session.commit()

        assert alias.id is not None
        assert alias.raw_payer_name == "Almaz Bekele (Spouse)"
        assert alias.normalized_payer_ref == "911998877"
        assert alias.deleted_at is None

        # Verify active listing
        active = list_payer_aliases(db_session, tenant_id=tenant_a.id)
        assert len(active) == 1
        assert active[0].id == alias.id

        # Verify audit event emitted
        evt_create = db_session.execute(
            select(Event).where(
                Event.tenant_id == tenant_a.id,
                Event.event_type == "payer_alias.created",
                Event.aggregate_id == alias.id,
            )
        ).scalar_one_or_none()
        assert evt_create is not None
        assert evt_create.payload["retailer_id"] == str(retailer.id)

        # Soft delete
        deleted = delete_payer_alias(
            db_session,
            tenant_id=tenant_a.id,
            alias_id=alias.id,
            user_id=owner_user_a.id,
        )
        db_session.commit()
        assert deleted is True

        # Verify no longer active
        active_after = list_payer_aliases(db_session, tenant_id=tenant_a.id)
        assert len(active_after) == 0

        # Verify audit event for deletion
        evt_del = db_session.execute(
            select(Event).where(
                Event.tenant_id == tenant_a.id,
                Event.event_type == "payer_alias.deleted",
                Event.aggregate_id == alias.id,
            )
        ).scalar_one_or_none()
        assert evt_del is not None

    def test_strict_invariance_payer_memory_never_auto_confirms(
        self,
        db_session: Session,
        tenant_a: Tenant,
        owner_user_a: User,
    ):
        """
        CRITICAL SAFETY RULE:
        Even if the evidence is L1 (webhook/verified) and invoice amount matches to the exact santim,
        payer memory proposals MUST ALWAYS have status='proposed' and NEVER 'auto_confirmed' or 'confirmed'.
        """
        set_tenant(db_session, tenant_a.id)

        retailer = Retailer(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            name="Invariance Retailer",
            created_at=_now(),
        )
        db_session.add(retailer)
        db_session.flush()

        inv = Invoice(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            invoice_number="INV-INV-001",
            retailer_id=retailer.id,
            amount_santim=250_000,
            issued_date=_today(),
            due_date=_today() + timedelta(days=15),
            state=InvoiceState.OPEN.value,
            created_at=_now(),
            created_by=owner_user_a.id,
        )
        db_session.add(inv)
        db_session.flush()

        alias = create_payer_alias(
            db_session,
            tenant_id=tenant_a.id,
            retailer_id=retailer.id,
            raw_payer_name="Brother Of Retailer",
            raw_payer_ref="+251911445566",
            user_id=owner_user_a.id,
        )
        db_session.commit()

        # Incoming payment line with L1 evidence (highest tier)
        pl = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            amount_santim=250_000,
            value_date=_today(),
            evidence_level=EvidenceLevel.L1.value,
            state="unmatched",
            counterparty_name="Brother Of Retailer",
            counterparty_account="+251911445566",
            created_at=_now(),
        )

        res = evaluate_payer_memory_proposal(
            db_session,
            tenant_id=tenant_a.id,
            payment_line=pl,
            open_invoices=[inv],
            retailers=[retailer],
        )

        assert res is not None
        # Strict Invariance Checks:
        assert res.status == "proposed", f"Expected 'proposed', got {res.status}"
        assert res.status != "auto_confirmed"
        assert res.status != "confirmed"
        today_str = alias.created_at.strftime("%Y-%m-%d")
        assert res.reason == f"Known payer, you confirmed on {today_str}"
        assert len(res.matches) == 1
        assert res.matches[0].invoice_id == inv.id

    def test_payer_alias_ambiguity_routes_to_review(
        self,
        db_session: Session,
        tenant_a: Tenant,
        owner_user_a: User,
    ):
        """When an owner links the same payer reference to two retailers, the match must be marked ambiguous."""
        set_tenant(db_session, tenant_a.id)

        ret1 = Retailer(id=uuid.uuid4(), tenant_id=tenant_a.id, name="Retailer One", created_at=_now())
        ret2 = Retailer(id=uuid.uuid4(), tenant_id=tenant_a.id, name="Retailer Two", created_at=_now())
        db_session.add_all([ret1, ret2])
        db_session.flush()

        # Same shared family phone linked to both retailers
        shared_phone = "0911556677"
        create_payer_alias(
            db_session,
            tenant_id=tenant_a.id,
            retailer_id=ret1.id,
            raw_payer_name="Shared Family Account",
            raw_payer_ref=shared_phone,
            user_id=owner_user_a.id,
        )
        create_payer_alias(
            db_session,
            tenant_id=tenant_a.id,
            retailer_id=ret2.id,
            raw_payer_name="Shared Family Account",
            raw_payer_ref=shared_phone,
            user_id=owner_user_a.id,
        )
        db_session.commit()

        pl = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            amount_santim=50_000,
            value_date=_today(),
            evidence_level="L2",
            state="unmatched",
            counterparty_name="Shared Family Account",
            counterparty_account=shared_phone,
            created_at=_now(),
        )

        res = evaluate_payer_memory_proposal(
            db_session,
            tenant_id=tenant_a.id,
            payment_line=pl,
            open_invoices=[],
            retailers=[ret1, ret2],
        )

        assert res is not None
        assert res.status == "proposed"
        assert res.is_ambiguous is True
        assert "Ambiguous: Payer mapped to multiple retailers" in res.reason
        assert "Retailer One" in res.reason
        assert "Retailer Two" in res.reason

    def test_amharic_name_matching_cross_script(
        self,
        db_session: Session,
        tenant_a: Tenant,
        owner_user_a: User,
    ):
        """Test Amharic Fidel script stored in alias matches Latin transliteration on statement line."""
        set_tenant(db_session, tenant_a.id)

        retailer = Retailer(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            name="Piassa Cosmetics",
            created_at=_now(),
        )
        db_session.add(retailer)
        db_session.flush()

        inv = Invoice(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            invoice_number="INV-AMH-01",
            retailer_id=retailer.id,
            amount_santim=180_000,
            issued_date=_today(),
            due_date=_today() + timedelta(days=10),
            state="open",
            created_at=_now(),
            created_by=owner_user_a.id,
        )
        db_session.add(inv)
        db_session.flush()

        # Link payer using Amharic Fidel: ታደሰ ወርቁ
        create_payer_alias(
            db_session,
            tenant_id=tenant_a.id,
            retailer_id=retailer.id,
            raw_payer_name="ታደሰ ወርቁ",
            raw_payer_ref=None,
            user_id=owner_user_a.id,
        )
        db_session.commit()

        # Incoming payment line from Telebirr in English: "Tadesse Worku"
        pl = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            amount_santim=180_000,
            value_date=_today(),
            evidence_level="L2",
            state="unmatched",
            counterparty_name="Tadesse Worku",
            created_at=_now(),
        )

        res = evaluate_payer_memory_proposal(
            db_session,
            tenant_id=tenant_a.id,
            payment_line=pl,
            open_invoices=[inv],
            retailers=[retailer],
        )

        assert res is not None
        assert res.status == "proposed"
        assert len(res.matches) == 1
        assert res.matches[0].invoice_id == inv.id


class TestPayerMemoryWebSecurityAndEndpoints:
    def test_payer_memory_tenant_isolation(
        self,
        db_session: Session,
        tenant_a: Tenant,
        tenant_b: Tenant,
        owner_user_a: User,
        owner_user_b: User,
    ):
        """Payer alias of Tenant A cannot be seen or matched by Tenant B."""
        set_tenant(db_session, tenant_a.id)
        ret_a = Retailer(id=uuid.uuid4(), tenant_id=tenant_a.id, name="Retailer A", created_at=_now())
        db_session.add(ret_a)
        db_session.flush()

        alias_a = create_payer_alias(
            db_session,
            tenant_id=tenant_a.id,
            retailer_id=ret_a.id,
            raw_payer_name="Secret Payer",
            raw_payer_ref="0911001122",
            user_id=owner_user_a.id,
        )
        db_session.commit()

        # Switch to Tenant B
        set_tenant(db_session, tenant_b.id)
        aliases_b = list_payer_aliases(db_session, tenant_id=tenant_b.id)
        assert len(aliases_b) == 0

        # Attempt evaluation under Tenant B with identical counterparty
        pl_b = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=tenant_b.id,
            amount_santim=10_000,
            value_date=_today(),
            evidence_level="L2",
            state="unmatched",
            counterparty_name="Secret Payer",
            counterparty_account="0911001122",
            created_at=_now(),
        )
        res_b = evaluate_payer_memory_proposal(
            db_session,
            tenant_id=tenant_b.id,
            payment_line=pl_b,
            open_invoices=[],
            retailers=[],
        )
        assert res_b is None

    def test_cashier_forbidden_from_payer_alias_routes(
        self,
        client: TestClient,
        db_session: Session,
        tenant_a: Tenant,
        cashier_user_a: User,
    ):
        """Cashier must be rejected with HTTP 403 on all payer alias endpoints."""
        authenticate_client(client, db_session, cashier_user_a)
        dummy_id = uuid.uuid4()

        # 1. GET listing
        resp_get = client.get("/payer-aliases")
        assert resp_get.status_code == 403

        # 2. POST create
        resp_post = client.post(
            "/payer-aliases",
            data={"retailer_id": str(dummy_id), "raw_payer_name": "Test Payer"},
        )
        assert resp_post.status_code == 403

        # 3. POST delete
        resp_del = client.post(f"/payer-aliases/{dummy_id}/delete")
        assert resp_del.status_code == 403

    def test_html_escaping_in_payer_alias_listing(
        self,
        client: TestClient,
        db_session: Session,
        tenant_a: Tenant,
        owner_user_a: User,
    ):
        """XSS payloads in payer name or reference are properly escaped in HTML rendering."""
        _, csrf_token = authenticate_client(client, db_session, owner_user_a)

        set_tenant(db_session, tenant_a.id)
        retailer = Retailer(id=uuid.uuid4(), tenant_id=tenant_a.id, name="Clean Retailer", created_at=_now())
        db_session.add(retailer)
        db_session.commit()

        xss_name = "<script>alert('pwned')</script>"
        xss_ref = "\"><svg/onload=alert(1)>"

        # Create alias via POST
        resp = client.post(
            "/payer-aliases",
            data={
                "csrf_token": csrf_token,
                "retailer_id": str(retailer.id),
                "raw_payer_name": xss_name,
                "raw_payer_ref": xss_ref,
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200

        # Assert unescaped script tag is NOT in the rendered response
        assert "<script>alert('pwned')</script>" not in resp.text
        assert "\"><svg/onload=alert(1)>" not in resp.text
        # Assert properly escaped forms are present
        assert "&lt;script&gt;alert(&#39;pwned&#39;)&lt;/script&gt;" in resp.text or "&lt;script&gt;" in resp.text

    def test_web_alias_lifecycle_create_and_delete(
        self,
        client: TestClient,
        db_session: Session,
        tenant_a: Tenant,
        owner_user_a: User,
    ):
        """End-to-end web test for owner creating and deleting an alias."""
        _, csrf_token = authenticate_client(client, db_session, owner_user_a)

        set_tenant(db_session, tenant_a.id)
        retailer = Retailer(id=uuid.uuid4(), tenant_id=tenant_a.id, name="Test Merkato Retailer", created_at=_now())
        db_session.add(retailer)
        db_session.commit()

        # 1. Create alias
        resp_create = client.post(
            "/payer-aliases",
            data={
                "csrf_token": csrf_token,
                "retailer_id": str(retailer.id),
                "raw_payer_name": "Ato Girma (Relative)",
                "raw_payer_ref": "+251911334455",
            },
            follow_redirects=True,
        )
        assert resp_create.status_code == 200
        assert "Ato Girma (Relative)" in resp_create.text
        assert "Test Merkato Retailer" in resp_create.text

        # Get created alias from DB
        alias = db_session.execute(
            select(PayerAlias).where(
                PayerAlias.tenant_id == tenant_a.id,
                PayerAlias.retailer_id == retailer.id,
                PayerAlias.deleted_at.is_(None),
            )
        ).scalar_one()

        # 2. Delete alias
        resp_del = client.post(
            f"/payer-aliases/{alias.id}/delete",
            data={"csrf_token": csrf_token},
            follow_redirects=True,
        )
        assert resp_del.status_code == 200
        assert "Payer alias deleted successfully" in resp_del.text
        assert "No Payer Aliases Linked Yet" in resp_del.text or "Ato Girma (Relative)" not in resp_del.text
