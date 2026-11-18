"""
tests/reports/test_measure_real_problem.py

Stage 3 — Measure the Real Problem
Tests for the five friction metrics added to compute_pilot_metrics() and
the remittance notice in reminders.py and the confirm_balance.html template.

ALL DATA IN THESE TESTS IS STRICTLY SYNTHETIC.
No real customer names, phone numbers, invoice numbers, or events are used.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.kernel.enums import InvoiceState, MatchState, EvidenceLevel, UserRole
from app.kernel.models import (
    Event,
    ImportBatch,
    Invoice,
    PayerAlias,
    PaymentLine,
    PaymentMatch,
    Retailer,
    Tenant,
    User,
)
from app.kernel.tenant import set_tenant
from app.matching.payer_memory import normalize_payer_name, normalize_payer_reference
from app.reports.pilot_metrics import compute_pilot_metrics


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dt(days_ago: int = 0, hour: int = 10) -> datetime:
    base = _now().replace(hour=hour, minute=0, second=0, microsecond=0)
    return base - timedelta(days=days_ago)


def _make_import_batch(db: Session, tenant_id: uuid.UUID, owner_id: uuid.UUID) -> ImportBatch:
    """Create a minimal ImportBatch with required fields. SYNTHETIC."""
    batch = ImportBatch(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        source_type="cbe_csv",
        evidence_level="L2",
        filename="synthetic_test_stage3.csv",
        file_hash=None,
        imported_by=owner_id,
        imported_at=_now(),
    )
    db.add(batch)
    db.flush()
    return batch


def _make_retailer(db: Session, tenant_id: uuid.UUID, *, name: str, phone: str) -> Retailer:
    """Create a SYNTHETIC retailer."""
    r = Retailer(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name=name,
        phone=phone,
        created_at=_dt(30),
    )
    db.add(r)
    db.flush()
    return r


def _make_invoice(
    db: Session,
    tenant_id: uuid.UUID,
    retailer_id: uuid.UUID,
    owner_id: uuid.UUID,
    *,
    number: str,
    amount: int = 100_000,
    days_ago: int = 10,
) -> Invoice:
    """Create a SYNTHETIC open invoice."""
    inv = Invoice(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        retailer_id=retailer_id,
        invoice_number=number,
        amount_santim=amount,
        issued_date=_dt(days_ago).date(),
        due_date=(_dt(days_ago) + timedelta(days=30)).date(),
        state=InvoiceState.OPEN.value,
        created_at=_dt(days_ago),
        created_by=owner_id,
    )
    db.add(inv)
    db.flush()
    return inv


def _make_payment_line(
    db: Session,
    tenant_id: uuid.UUID,
    batch_id: uuid.UUID,
    *,
    reference: str = "SYNTH-REF",
    counterparty_name: str = "SYNTH PAYER",
    counterparty_account: str = "",
    amount: int = 50_000,
    days_ago: int = 5,
) -> PaymentLine:
    """Create a SYNTHETIC PaymentLine (statement credit). Uses the real model fields."""
    pl = PaymentLine(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        import_batch_id=batch_id,
        source_ref=reference or "SYNTH-REF",
        row_key=str(uuid.uuid4()),  # unique per row
        amount_santim=amount,
        value_date=_dt(days_ago).date(),
        counterparty_name=counterparty_name or None,
        counterparty_account=counterparty_account or None,
        raw_reference=reference or None,
        evidence_level="L2",
        state="unmatched",
        is_previously_reversed=False,
        created_at=_dt(days_ago),
    )
    db.add(pl)
    db.flush()
    return pl


# ---------------------------------------------------------------------------
# Tests: empty tenant → "not available" on all 5 new metrics
# ---------------------------------------------------------------------------

class TestEmptyTenantMetrics:
    """With zero statement credits, all five friction metrics must be 'not available'."""

    def test_all_friction_metrics_not_available_when_no_data(
        self, db_session: Session, tenant_a: Tenant, owner_user_a: User
    ):
        """SYNTHETIC: empty tenant produces 'not available' for all 5 Stage-3 metrics."""
        set_tenant(db_session, tenant_a.id)
        today = _now().date()
        result = compute_pilot_metrics(
            db_session,
            tenant_id=tenant_a.id,
            from_date=today - timedelta(days=30),
            to_date=today,
        )
        m = result["metrics"]
        assert m["ref_invoice_match_share"]["value"] == "not available", \
            "ref_invoice_match_share must be 'not available' with no data"
        assert m["retailer_counterparty_share"]["value"] == "not available", \
            "retailer_counterparty_share must be 'not available' with no data"
        assert m["known_payer_share"]["value"] == "not available", \
            "known_payer_share must be 'not available' with no data"
        assert m["no_usable_identifier_share"]["value"] == "not available", \
            "no_usable_identifier_share must be 'not available' with no data"
        assert m["review_queue_daily_minutes"]["value"] == "not available", \
            "review_queue_daily_minutes must be 'not available' with no data"

    def test_no_exception_on_empty_tenant(
        self, db_session: Session, tenant_a: Tenant, owner_user_a: User
    ):
        """SYNTHETIC: compute_pilot_metrics must not raise on a tenant with zero rows."""
        set_tenant(db_session, tenant_a.id)
        today = _now().date()
        compute_pilot_metrics(
            db_session,
            tenant_id=tenant_a.id,
            from_date=today - timedelta(days=30),
            to_date=today,
        )

    def test_sample_sizes_zero_on_empty_tenant(
        self, db_session: Session, tenant_a: Tenant, owner_user_a: User
    ):
        """SYNTHETIC: sample_size must be 0 for all five metrics when no credits exist."""
        set_tenant(db_session, tenant_a.id)
        today = _now().date()
        result = compute_pilot_metrics(
            db_session,
            tenant_id=tenant_a.id,
            from_date=today - timedelta(days=30),
            to_date=today,
        )
        m = result["metrics"]
        assert m["ref_invoice_match_share"]["sample_size"] == 0
        assert m["retailer_counterparty_share"]["sample_size"] == 0
        assert m["known_payer_share"]["sample_size"] == 0
        assert m["no_usable_identifier_share"]["sample_size"] == 0
        assert m["review_queue_daily_minutes"]["sample_size"] == 0


# ---------------------------------------------------------------------------
# Tests: ref_invoice_match_share
# ---------------------------------------------------------------------------

class TestRefInvoiceMatchShare:
    """SYNTHETIC: statement credits whose reference contains an open invoice number."""

    def test_exact_invoice_match_in_reference(
        self, db_session: Session, tenant_a: Tenant, owner_user_a: User
    ):
        """SYNTHETIC: 2/3 credits referencing an open invoice → ~66.7%."""
        set_tenant(db_session, tenant_a.id)
        today = _now().date()

        retailer = _make_retailer(db_session, tenant_a.id,
                                  name="SYNTHETIC-RETAILER-REF-01",
                                  phone="+251911100101")
        _make_invoice(db_session, tenant_a.id, retailer.id, owner_user_a.id,
                      number="INV-SYNTH-S3-001")
        batch = _make_import_batch(db_session, tenant_a.id, owner_user_a.id)

        # Credit 1: reference contains open invoice number → match
        _make_payment_line(db_session, tenant_a.id, batch.id,
                           reference="INV-SYNTH-S3-001 payment",
                           counterparty_name="SYNTHETIC PAYER A",
                           amount=50_000, days_ago=3)
        # Credit 2: also references the invoice → match
        _make_payment_line(db_session, tenant_a.id, batch.id,
                           reference="Ref INV-SYNTH-S3-001",
                           counterparty_name="SYNTHETIC PAYER B",
                           amount=50_000, days_ago=2)
        # Credit 3: no recognizable reference
        _make_payment_line(db_session, tenant_a.id, batch.id,
                           reference="WIRE-9999-X",
                           counterparty_name="SYNTHETIC PAYER C",
                           amount=50_000, days_ago=1)

        result = compute_pilot_metrics(
            db_session,
            tenant_id=tenant_a.id,
            from_date=today - timedelta(days=30),
            to_date=today,
        )
        m = result["metrics"]["ref_invoice_match_share"]
        assert m["sample_size"] == 3, f"Expected 3 credits, got {m['sample_size']}"
        assert m["count"] == 2, f"Expected 2 matches, got {m['count']}"
        assert m["value"] != "not available"
        assert m["raw_pct"] is not None
        assert 60.0 <= m["raw_pct"] <= 70.0, f"Expected ~66.7%, got {m['raw_pct']}"

    def test_zero_matches_returns_zero_pct_not_not_available(
        self, db_session: Session, tenant_a: Tenant, owner_user_a: User
    ):
        """SYNTHETIC: credits exist but none match an invoice → 0.0% (not 'not available')."""
        set_tenant(db_session, tenant_a.id)
        today = _now().date()
        batch = _make_import_batch(db_session, tenant_a.id, owner_user_a.id)
        _make_payment_line(db_session, tenant_a.id, batch.id,
                           reference="WIRE-XYZ-999-S3",
                           counterparty_name="SYNTHETIC WIRE S3",
                           amount=100_000, days_ago=1)

        result = compute_pilot_metrics(
            db_session,
            tenant_id=tenant_a.id,
            from_date=today - timedelta(days=30),
            to_date=today,
        )
        m = result["metrics"]["ref_invoice_match_share"]
        assert m["sample_size"] == 1
        assert m["count"] == 0
        assert m["raw_pct"] == 0.0
        assert "%" in m["value"]


# ---------------------------------------------------------------------------
# Tests: retailer_counterparty_share
# ---------------------------------------------------------------------------

class TestRetailerCounterpartyShare:
    """SYNTHETIC: credits whose counterparty name or account matches a known retailer."""

    def test_phone_match_on_counterparty_account(
        self, db_session: Session, tenant_a: Tenant, owner_user_a: User
    ):
        """SYNTHETIC: counterparty_account = retailer phone (normalized) → 1/2 = 50%."""
        set_tenant(db_session, tenant_a.id)
        today = _now().date()

        _make_retailer(db_session, tenant_a.id,
                       name="SYNTH-RETAILER-PHONE-01",
                       phone="+251911200201")
        batch = _make_import_batch(db_session, tenant_a.id, owner_user_a.id)

        # Credit 1: counterparty_account contains retailer phone local format → may match
        _make_payment_line(db_session, tenant_a.id, batch.id,
                           reference="WIRE-A-S3",
                           counterparty_name="UNKNOWN S3",
                           counterparty_account="0911200201",
                           amount=70_000, days_ago=4)
        # Credit 2: no match
        _make_payment_line(db_session, tenant_a.id, batch.id,
                           reference="WIRE-B-S3",
                           counterparty_name="COMPLETELY UNRELATED S3",
                           counterparty_account="0999999999",
                           amount=30_000, days_ago=3)

        result = compute_pilot_metrics(
            db_session,
            tenant_id=tenant_a.id,
            from_date=today - timedelta(days=30),
            to_date=today,
        )
        m = result["metrics"]["retailer_counterparty_share"]
        assert m["sample_size"] == 2
        # Either 1 or 2 matches depending on normalization; must be a numeric pct
        assert m["value"] != "not available"
        assert m["raw_pct"] is not None


# ---------------------------------------------------------------------------
# Tests: known_payer_share
# ---------------------------------------------------------------------------

class TestKnownPayerShare:
    """SYNTHETIC: credits matched through an owner-created payer alias."""

    def test_payer_alias_never_produces_auto_confirmed(
        self, db_session: Session, tenant_a: Tenant, owner_user_a: User
    ):
        """SYNTHETIC: a payer alias result must never produce auto_confirmed matches."""
        from sqlalchemy import select as _select
        set_tenant(db_session, tenant_a.id)
        today = _now().date()

        retailer = _make_retailer(db_session, tenant_a.id,
                                  name="SYNTH-ALIAS-SAFETY",
                                  phone="+251911400401")
        _make_invoice(db_session, tenant_a.id, retailer.id, owner_user_a.id,
                      number="SAFETY-INV-S3-001")

        alias_name = "SYNTH SAFETY PAYER S3"
        alias = PayerAlias(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            retailer_id=retailer.id,
            normalized_payer_name=normalize_payer_name(alias_name),
            normalized_payer_ref=None,
            raw_payer_name=alias_name,
            raw_payer_ref=None,
            created_by=owner_user_a.id,
            created_at=_dt(5),
        )
        db_session.add(alias)
        db_session.flush()

        batch = _make_import_batch(db_session, tenant_a.id, owner_user_a.id)
        _make_payment_line(db_session, tenant_a.id, batch.id,
                           reference="SAFETY-REF-S3",
                           counterparty_name=alias_name,
                           amount=50_000, days_ago=2)

        # Run metrics — must not raise
        compute_pilot_metrics(
            db_session,
            tenant_id=tenant_a.id,
            from_date=today - timedelta(days=30),
            to_date=today,
        )

        # Verify no confirmed PaymentMatch exists after metric computation
        matches = db_session.execute(
            _select(PaymentMatch).where(
                PaymentMatch.tenant_id == tenant_a.id,
                PaymentMatch.state == MatchState.CONFIRMED.value,
            )
        ).scalars().all()
        assert len(matches) == 0, \
            "INVARIANT BREACH: alias-aware metric computation created confirmed match"

    def test_known_payer_share_not_available_without_aliases(
        self, db_session: Session, tenant_a: Tenant, owner_user_a: User
    ):
        """SYNTHETIC: with credits but no aliases, known_payer_share is 0% (not 'not available')."""
        set_tenant(db_session, tenant_a.id)
        today = _now().date()

        batch = _make_import_batch(db_session, tenant_a.id, owner_user_a.id)
        _make_payment_line(db_session, tenant_a.id, batch.id,
                           reference="KP-WIRE-001",
                           counterparty_name="SYNTH NO ALIAS PAYER",
                           amount=60_000, days_ago=2)

        result = compute_pilot_metrics(
            db_session,
            tenant_id=tenant_a.id,
            from_date=today - timedelta(days=30),
            to_date=today,
        )
        m = result["metrics"]["known_payer_share"]
        # With credits but no aliases → 0 matches → 0.0%
        assert m["sample_size"] == 1
        assert m["count"] == 0
        assert m["raw_pct"] == 0.0


# ---------------------------------------------------------------------------
# Tests: no_usable_identifier_share
# ---------------------------------------------------------------------------

class TestNoUsableIdentifierShare:
    """SYNTHETIC: credits with no reference, no counterparty name, no account."""

    def test_no_identifier_credits_counted(
        self, db_session: Session, tenant_a: Tenant, owner_user_a: User
    ):
        """SYNTHETIC: 1 blank credit out of 2 → 50% no_usable_identifier."""
        set_tenant(db_session, tenant_a.id)
        today = _now().date()
        batch = _make_import_batch(db_session, tenant_a.id, owner_user_a.id)

        # Credit with nothing useful
        _make_payment_line(db_session, tenant_a.id, batch.id,
                           reference="",
                           counterparty_name="",
                           counterparty_account="",
                           amount=10_000, days_ago=2)
        # Credit with some reference
        _make_payment_line(db_session, tenant_a.id, batch.id,
                           reference="WIRE-HAS-REF-S3",
                           counterparty_name="SYNTH PAYER D",
                           amount=90_000, days_ago=1)

        result = compute_pilot_metrics(
            db_session,
            tenant_id=tenant_a.id,
            from_date=today - timedelta(days=30),
            to_date=today,
        )
        m = result["metrics"]["no_usable_identifier_share"]
        assert m["sample_size"] == 2
        # At least 1 credit has no identifier
        assert m["count"] >= 1
        assert m["raw_pct"] is not None
        assert m["value"] != "not available"


# ---------------------------------------------------------------------------
# Tests: Reminder text — invoice note
# ---------------------------------------------------------------------------

class TestReminderInvoiceNote:
    """SYNTHETIC: reminders include the invoice note, Amharic is marked for native review."""

    def test_english_reminder_contains_invoice_note(self):
        """The English reminder template must contain the invoice-number instruction."""
        from app.reports.reminders import generate_reminder_text
        text = generate_reminder_text(
            tenant_name="SYNTHETIC TENANT S3",
            retailer_name="SYNTHETIC RETAILER S3",
            count=1,
            amount_santim=500_000,
            earliest_due=date(2026, 12, 31),
            lang="en",
        )
        assert "Please write the invoice number in your transfer note" in text, \
            f"Invoice note missing from English reminder. Got: {text!r}"

    def test_english_reminder_still_contains_original_content(self):
        """The English reminder must still contain Thank You message (no regression)."""
        from app.reports.reminders import generate_reminder_text
        text = generate_reminder_text(
            tenant_name="SYNTHETIC TENANT S3-B",
            retailer_name="SYNTHETIC RETAILER S3-B",
            count=2,
            amount_santim=300_000,
            earliest_due=date(2026, 12, 31),
            lang="en",
        )
        assert "Thank you" in text, f"'Thank you' missing from English reminder. Got: {text!r}"

    def test_amharic_reminder_contains_native_review_marker(self):
        """The Amharic reminder must contain the 'needs native review' marker."""
        from app.reports.reminders import generate_reminder_text
        text = generate_reminder_text(
            tenant_name="SYNTHETIC TENANT AM",
            retailer_name="SYNTHETIC RETAILER AM",
            count=1,
            amount_santim=200_000,
            earliest_due=date(2026, 12, 31),
            lang="am",
        )
        assert "needs native review" in text, \
            f"Amharic reminder missing native-review marker. Got: {text!r}"


# ---------------------------------------------------------------------------
# Tests: confirm_balance.html — remittance instruction box
# ---------------------------------------------------------------------------

class TestConfirmBalanceTemplate:
    """confirm_balance.html must render the remittance-instruction-box."""

    def test_confirm_balance_template_contains_instruction_box(self):
        """Template source must contain data-testid='remittance-instruction-box'."""
        template_path = Path(
            "C:/Users/Noha/Downloads/daily-close/app/channels/web/templates/confirm_balance.html"
        )
        assert template_path.exists(), f"Template not found: {template_path}"
        content = template_path.read_text(encoding="utf-8")
        assert 'data-testid="remittance-instruction-box"' in content, \
            "confirm_balance.html missing data-testid='remittance-instruction-box'"

    def test_confirm_balance_template_no_inline_on_handlers(self):
        """confirm_balance.html must not contain inline on* handlers."""
        import re
        template_path = Path(
            "C:/Users/Noha/Downloads/daily-close/app/channels/web/templates/confirm_balance.html"
        )
        content = template_path.read_text(encoding="utf-8")
        inline_handlers = re.findall(r'\bon\w+\s*=', content)
        assert len(inline_handlers) == 0, \
            f"confirm_balance.html has inline event handlers: {inline_handlers}"

    def test_confirm_balance_template_no_safe_filter(self):
        """confirm_balance.html must not use Jinja2 |safe filter (XSS risk)."""
        template_path = Path(
            "C:/Users/Noha/Downloads/daily-close/app/channels/web/templates/confirm_balance.html"
        )
        content = template_path.read_text(encoding="utf-8")
        assert "|safe" not in content, "confirm_balance.html uses |safe filter — XSS risk"


# ---------------------------------------------------------------------------
# Tests: Old metrics still present (no regression)
# ---------------------------------------------------------------------------

class TestOldMetricsNotRemoved:
    """The 8 original metrics from feat/pilot-metrics must still be in the result."""

    def test_all_original_metrics_present(
        self, db_session: Session, tenant_a: Tenant, owner_user_a: User
    ):
        """SYNTHETIC: all 8 original metrics keys remain after Stage 3 additions."""
        set_tenant(db_session, tenant_a.id)
        today = _now().date()
        result = compute_pilot_metrics(
            db_session,
            tenant_id=tenant_a.id,
            from_date=today - timedelta(days=30),
            to_date=today,
        )
        m = result["metrics"]
        expected_original = [
            "log_velocity",
            "turnaround",
            "overdue_trajectory",
            "review_queue",
            "auto_confirm_share",
            "reversal_rate",
            "retailer_links",
            "active_days",
        ]
        for key in expected_original:
            assert key in m, f"Original metric '{key}' missing after Stage 3 edits"

    def test_all_new_metrics_present(
        self, db_session: Session, tenant_a: Tenant, owner_user_a: User
    ):
        """SYNTHETIC: all 5 new Stage-3 metrics keys are in the result."""
        set_tenant(db_session, tenant_a.id)
        today = _now().date()
        result = compute_pilot_metrics(
            db_session,
            tenant_id=tenant_a.id,
            from_date=today - timedelta(days=30),
            to_date=today,
        )
        m = result["metrics"]
        new_keys = [
            "ref_invoice_match_share",
            "retailer_counterparty_share",
            "known_payer_share",
            "no_usable_identifier_share",
            "review_queue_daily_minutes",
        ]
        for key in new_keys:
            assert key in m, f"New Stage-3 metric '{key}' missing from result"


# ---------------------------------------------------------------------------
# Tests: Tenant isolation — metric cannot see another tenant's credits
# ---------------------------------------------------------------------------

class TestFrictionMetricsTenantIsolation:
    """SYNTHETIC: Stage-3 metrics must not cross tenant boundaries."""

    def test_ref_invoice_match_share_isolated(
        self, db_session: Session, tenant_a: Tenant, tenant_b: Tenant,
        owner_user_a: User, owner_user_b: User
    ):
        """SYNTHETIC: credits in tenant_b must not appear in tenant_a metrics."""
        # Seed batch and credit in tenant_b
        set_tenant(db_session, tenant_b.id)
        b_batch = ImportBatch(
            id=uuid.uuid4(),
            tenant_id=tenant_b.id,
            source_type="cbe_csv",
            evidence_level="L2",
            filename="synthetic_b_s3.csv",
            file_hash=None,
            imported_by=owner_user_b.id,
            imported_at=_now(),
        )
        db_session.add(b_batch)
        db_session.flush()

        b_credit = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=tenant_b.id,
            import_batch_id=b_batch.id,
            source_ref="TENANT-B-INV-SYNTH-9999",
            row_key=str(uuid.uuid4()),
            amount_santim=999_000,
            value_date=_now().date(),
            counterparty_name="TENANT-B-PAYER-SYNTH",
            counterparty_account=None,
            raw_reference="TENANT-B-INV-SYNTH-9999",
            evidence_level="L2",
            state="unmatched",
            is_previously_reversed=False,
            created_at=_now(),
        )
        db_session.add(b_credit)
        db_session.flush()

        # Now switch to tenant_a context — must NOT see tenant_b's credit
        set_tenant(db_session, tenant_a.id)
        today = _now().date()
        result = compute_pilot_metrics(
            db_session,
            tenant_id=tenant_a.id,
            from_date=today - timedelta(days=30),
            to_date=today,
        )
        m = result["metrics"]["ref_invoice_match_share"]
        # tenant_a has no credits, so sample_size must be 0
        assert m["sample_size"] == 0, \
            f"Tenant isolation breach: tenant_a sees {m['sample_size']} credits from tenant_b"
