"""
tests/matching/test_matching_rules.py

Unit tests for matching rules and invariants:
  - Rule 2: Evidence ladder (L1/L2/L3 confirmable, L4 proposed only)
  - Rule 3: Ambiguity guard (two or more plausible invoices -> review queue)
  - Partial payments and combined payments proposed only
"""

import uuid
from datetime import date, timedelta

import pytest

from app.kernel.enums import EvidenceLevel, MatchTier
from app.kernel.guards import InsufficientEvidenceError, assert_confirmable
from app.kernel.models import Invoice, PaymentLine, Retailer
from app.matching.matcher import MatcherConfig, PaymentMatcher


def _make_retailer(name="Abebe Shop", phone="0911234567", acc="10001234567") -> Retailer:
    return Retailer(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        name=name,
        phone=phone,
        bank_account=acc,
        created_at=date(2026, 1, 1),
    )


def _make_invoice(
    retailer: Retailer,
    number="INV-001",
    amount=5000000,
    due_date=date(2026, 8, 30),
) -> Invoice:
    return Invoice(
        id=uuid.uuid4(),
        tenant_id=retailer.tenant_id,
        invoice_number=number,
        retailer_id=retailer.id,
        amount_santim=amount,
        issued_date=due_date - timedelta(days=30),
        due_date=due_date,
        state="open",
        created_at=due_date - timedelta(days=30),
        created_by=uuid.uuid4(),
    )


class TestMatchingRules:
    def setup_method(self):
        self.matcher = PaymentMatcher()

    # -----------------------------------------------------------------------
    # Tier A Tests
    # -----------------------------------------------------------------------
    def test_tier_a_auto_confirm(self):
        ret = _make_retailer()
        inv = _make_invoice(ret, number="INV-101", amount=1200000)

        pl = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=ret.tenant_id,
            amount_santim=1200000,
            value_date=date(2026, 8, 25),
            raw_reference="Settlement for INV-101 via CBE",
            evidence_level="L2",
            state="unmatched",
            created_at=date(2026, 8, 25),
        )

        res = self.matcher.match_payment(pl, [inv], [ret])
        assert res.status == "auto_confirmed"
        assert res.tier == MatchTier.TIER_A
        assert len(res.matches) == 1
        assert res.matches[0].invoice_id == inv.id
        assert not res.is_ambiguous

    def test_tier_a_l4_evidence_never_auto_confirms(self):
        """Rule 2: L4 evidence MUST NEVER auto-confirm."""
        ret = _make_retailer()
        inv = _make_invoice(ret, number="INV-102", amount=1200000)

        pl = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=ret.tenant_id,
            amount_santim=1200000,
            value_date=date(2026, 8, 25),
            raw_reference="INV-102 photo of receipt",
            evidence_level="L4",  # L4 screenshot/note
            state="unmatched",
            created_at=date(2026, 8, 25),
        )

        res = self.matcher.match_payment(pl, [inv], [ret])
        assert res.status == "proposed"  # L4 is only proposed!
        assert res.tier == MatchTier.TIER_A

    def test_tier_a_ambiguity_two_invoices_matched(self):
        """Rule 3: Multiple plausible invoices -> review queue."""
        ret = _make_retailer()
        # Two invoices mentioned in text or colliding
        inv1 = _make_invoice(ret, number="INV-100", amount=500000)
        inv2 = _make_invoice(ret, number="INV-101", amount=500000)

        pl = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=ret.tenant_id,
            amount_santim=500000,
            value_date=date(2026, 8, 25),
            raw_reference="Payment for INV-100 and INV-101",
            evidence_level="L2",
            state="unmatched",
            created_at=date(2026, 8, 25),
        )

        res = self.matcher.match_payment(pl, [inv1, inv2], [ret])
        assert res.status == "proposed"
        assert res.is_ambiguous is True

    # -----------------------------------------------------------------------
    # Partial & Combined Payments
    # -----------------------------------------------------------------------
    def test_partial_payment_proposed_only(self):
        ret = _make_retailer()
        inv = _make_invoice(ret, number="INV-200", amount=10000000)  # 100,000 ETB

        # Payment of 40,000 ETB
        pl = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=ret.tenant_id,
            amount_santim=4000000,
            value_date=date(2026, 8, 25),
            raw_reference="Partial deposit INV-200",
            evidence_level="L2",
            state="unmatched",
            created_at=date(2026, 8, 25),
        )

        res = self.matcher.match_payment(pl, [inv], [ret])
        assert res.status == "proposed"  # Partial payment MUST NOT auto-confirm
        assert res.matches[0].is_partial is True
        assert res.matches[0].matched_santim == 4000000

    def test_combined_payment_proposed_only(self):
        ret = _make_retailer()
        inv1 = _make_invoice(ret, number="INV-301", amount=3000000)  # 30,000 ETB
        inv2 = _make_invoice(ret, number="INV-302", amount=2000000)  # 20,000 ETB

        # Single payment of 50,000 ETB covering both
        pl = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=ret.tenant_id,
            amount_santim=5000000,
            value_date=date(2026, 8, 25),
            counterparty_account=ret.bank_account,
            raw_reference="Deposit",
            evidence_level="L2",
            state="unmatched",
            created_at=date(2026, 8, 25),
        )

        res = self.matcher.match_payment(pl, [inv1, inv2], [ret])
        assert res.status == "proposed"  # Combined payment MUST NOT auto-confirm
        assert len(res.matches) == 2
        assert {m.invoice_id for m in res.matches} == {inv1.id, inv2.id}

    # -----------------------------------------------------------------------
    # Tier B Tests (SEC-11: Tier B is routed to review queue as proposed)
    # -----------------------------------------------------------------------
    def test_tier_b_counterparty_phone_match(self):
        ret = _make_retailer(phone="0911223344")
        inv = _make_invoice(ret, number="INV-401", amount=7500000)

        pl = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=ret.tenant_id,
            amount_santim=7500000,
            value_date=date(2026, 8, 25),
            counterparty_name="Unknown Remitter",
            counterparty_account="+251911223344",  # Normalized phone matches
            raw_reference=None,  # No reference at all
            evidence_level="L2",
            state="unmatched",
            created_at=date(2026, 8, 25),
        )

        res = self.matcher.match_payment(pl, [inv], [ret])
        assert res.status == "proposed"
        assert res.tier == MatchTier.TIER_B
        assert res.matches[0].invoice_id == inv.id

    def test_tier_b_same_amount_collision_routes_to_review_queue(self):
        """Rule 3: Same retailer has 2 invoices with identical amount in date window."""
        ret = _make_retailer()
        inv1 = _make_invoice(ret, number="INV-501", amount=2000000)
        inv2 = _make_invoice(ret, number="INV-502", amount=2000000)

        pl = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=ret.tenant_id,
            amount_santim=2000000,
            value_date=date(2026, 8, 25),
            counterparty_account=ret.phone,
            raw_reference=None,
            evidence_level="L2",
            state="unmatched",
            created_at=date(2026, 8, 25),
        )

        res = self.matcher.match_payment(pl, [inv1, inv2], [ret])
        assert res.status == "proposed"
        assert res.is_ambiguous is True

    # -----------------------------------------------------------------------
    # Tier C Tests (SEC-11: Tier C is routed to review queue as proposed)
    # -----------------------------------------------------------------------
    def test_tier_c_fuzzy_name_match(self):
        ret = _make_retailer(name="Kassahun Desta General Store")
        inv = _make_invoice(ret, number="INV-601", amount=1500000)

        pl = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=ret.tenant_id,
            amount_santim=1500000,
            value_date=date(2026, 8, 25),
            counterparty_name="Kasahun Desta Shop",  # Transliteration variant
            counterparty_account=None,
            raw_reference=None,
            evidence_level="L2",
            state="unmatched",
            created_at=date(2026, 8, 25),
        )

        res = self.matcher.match_payment(pl, [inv], [ret])
        assert res.status == "proposed"
        assert res.tier == MatchTier.TIER_C
        assert res.matches[0].invoice_id == inv.id

    # -----------------------------------------------------------------------
    # Performance Test: 2,000 payments against 1,000 invoices under 60 seconds
    # -----------------------------------------------------------------------
    @pytest.mark.slow
    def test_matching_performance_2000_payments_1000_invoices(self):
        """
        Performance test: Reconciling 2,000 payment lines against 1,000 active invoices
        must complete well under 60 seconds.
        """
        import time
        import random

        rng = random.Random(42)
        retailers = [
            _make_retailer(name=f"Retailer {i} Store", phone=f"0911{i:05d}", acc=f"1000{i:06d}")
            for i in range(200)
        ]
        invoices = [
            _make_invoice(
                retailers[i % len(retailers)],
                number=f"INV-{i:04d}",
                amount=rng.randint(5000, 200000),
            )
            for i in range(1000)
        ]

        payments = []
        retailer_by_id = {r.id: r for r in retailers}
        for j in range(2000):
            target_inv = invoices[j % len(invoices)]
            target_ret = retailer_by_id[target_inv.retailer_id]
            mode = j % 4
            if mode == 0:
                ref = f"Payment for {target_inv.invoice_number}"
                c_name, c_acc = None, None
            elif mode == 1:
                ref = None
                c_name = None
                c_acc = target_ret.phone
            elif mode == 2:
                ref = None
                c_name = target_ret.name.replace("Store", "Shop")
                c_acc = None
            else:
                ref = f"Tax/Utility TX{j}"
                c_name, c_acc = "Unknown Vendor", None

            pl = PaymentLine(
                id=uuid.uuid4(),
                tenant_id=target_inv.tenant_id,
                amount_santim=target_inv.amount_santim if mode < 3 else rng.randint(1000, 50000),
                value_date=date(2026, 8, 25),
                counterparty_name=c_name,
                counterparty_account=c_acc,
                raw_reference=ref,
                evidence_level="L2",
                state="unmatched",
                created_at=date(2026, 8, 25),
            )
            payments.append(pl)

        start_time = time.perf_counter()
        results = [self.matcher.match_payment(pl, invoices, retailers) for pl in payments]
        duration = time.perf_counter() - start_time

        assert len(results) == 2000
        assert duration < 60.0, f"Performance test exceeded 60s limit: took {duration:.2f}s"
        print(f"\n[PERFORMANCE TEST] Matched 2,000 payments against 1,000 invoices in {duration:.2f} seconds ({2000/duration:.1f} payments/sec). Target: < 60s.")
