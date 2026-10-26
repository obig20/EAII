"""
tests/adversarial/benchmark_metrics.py

Independent benchmark evaluation metrics for Daily Close payment matching.
Calculates acceptance metrics using ONLY ground-truth fields:
- payments with a true invoice
- auto-confirmed correctly
- false confirmations (must be 0)
- queued with the true invoice at top-1
- queued with the true invoice at top-3 (ranks 2-3)
- true invoice not in top-3 (rank > 3)
- true invoice with no candidate at all
- payments with no true invoice (spurious)

Targets:
1. False confirmations: EXACTLY 0 on all seeds.
2. Coverage (auto_correct + top-1 + top-3) / payments_with_true >= 95.0%.
3. Top-1 proposal accuracy: top-1 / total_queued >= 85.0%.
4. Strict conservation: all counts add up to total payments.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Sequence

from app.kernel.enums import InvoiceState, PaymentState
from app.kernel.models import Invoice, PaymentLine, Retailer
from app.matching.matcher import PaymentMatcher
from app.matching.synthetic import SyntheticDataSet, generate_synthetic_dataset
from tests.adversarial.independent_generator import IndependentDataset, IndependentSyntheticGenerator

BENCHMARK_SEEDS = [1, 42, 100, 314, 777, 1024, 2026, 4096, 8192, 9999]


@dataclass
class NoiseCategoryMetric:
    noise_type: str
    total: int = 0
    with_true_invoice: int = 0
    auto_confirmed_correctly: int = 0
    false_confirmations: int = 0
    queued_top1: int = 0
    queued_top3: int = 0  # ranks 2..3
    queued_not_in_top3: int = 0
    no_candidate_at_all: int = 0
    spurious: int = 0


@dataclass
class BenchmarkSeedResult:
    seed: int
    generator_name: str
    total_payments: int = 0
    payments_with_true_invoice: int = 0
    auto_confirmed_correctly: int = 0
    false_confirmations: int = 0
    queued_top1: int = 0
    queued_top3: int = 0  # ranks 2..3
    queued_not_in_top3: int = 0
    no_candidate_at_all: int = 0
    spurious_payments: int = 0
    noise_breakdown: dict[str, NoiseCategoryMetric] = field(default_factory=dict)

    @property
    def coverage(self) -> float:
        if self.payments_with_true_invoice == 0:
            return 1.0
        return (
            self.auto_confirmed_correctly + self.queued_top1 + self.queued_top3
        ) / self.payments_with_true_invoice

    @property
    def queued_total(self) -> int:
        return self.queued_top1 + self.queued_top3 + self.queued_not_in_top3

    @property
    def top1_rate(self) -> float:
        q_tot = self.queued_total
        if q_tot == 0:
            return 1.0
        return self.queued_top1 / q_tot

    def verify_conservation(self) -> None:
        """Verify counts strictly add up to total payments."""
        assert (
            self.payments_with_true_invoice + self.spurious_payments == self.total_payments
        ), (
            f"Seed {self.seed}: Conservation broken! "
            f"True ({self.payments_with_true_invoice}) + Spurious ({self.spurious_payments}) != Total ({self.total_payments})"
        )
        assert (
            self.auto_confirmed_correctly
            + self.false_confirmations
            + self.queued_top1
            + self.queued_top3
            + self.queued_not_in_top3
            + self.no_candidate_at_all
            == self.payments_with_true_invoice
        ), (
            f"Seed {self.seed}: Category counts do not add up to payments with true invoice! "
            f"Sum != {self.payments_with_true_invoice}"
        )


def evaluate_synthetic_seed(
    matcher: PaymentMatcher,
    dataset: SyntheticDataSet,
    seed: int,
) -> BenchmarkSeedResult:
    """Evaluate synthetic dataset using only ground-truth fields."""
    result = BenchmarkSeedResult(seed=seed, generator_name="app/matching/synthetic.py")
    result.total_payments = len(dataset.payments_with_truth)

    for pt in dataset.payments_with_truth:
        ntype = pt.noise_type
        if ntype not in result.noise_breakdown:
            result.noise_breakdown[ntype] = NoiseCategoryMetric(noise_type=ntype)
        nm = result.noise_breakdown[ntype]
        nm.total += 1

        exp_ids = set(pt.expected_invoice_ids)
        res = matcher.match_payment(pt.payment_line, dataset.invoices, dataset.retailers)
        cand_ids = [m.invoice_id for m in res.matches]

        if not exp_ids:
            result.spurious_payments += 1
            nm.spurious += 1
            if res.status == "auto_confirmed":
                result.false_confirmations += 1
                nm.false_confirmations += 1
            continue

        result.payments_with_true_invoice += 1
        nm.with_true_invoice += 1

        if res.status == "auto_confirmed":
            is_valid = (
                pt.is_auto_confirmable
                and pt.payment_line.evidence_level != "L4"
                and set(cand_ids) == exp_ids
            )
            if is_valid:
                result.auto_confirmed_correctly += 1
                nm.auto_confirmed_correctly += 1
            else:
                result.false_confirmations += 1
                nm.false_confirmations += 1
        elif res.status == "proposed":
            if cand_ids and cand_ids[0] in exp_ids:
                result.queued_top1 += 1
                nm.queued_top1 += 1
            elif any(cid in exp_ids for cid in cand_ids[1:3]):
                result.queued_top3 += 1
                nm.queued_top3 += 1
            elif any(cid in exp_ids for cid in cand_ids[3:]):
                result.queued_not_in_top3 += 1
                nm.queued_not_in_top3 += 1
            else:
                result.no_candidate_at_all += 1
                nm.no_candidate_at_all += 1
        else:  # unmatched
            result.no_candidate_at_all += 1
            nm.no_candidate_at_all += 1

    result.verify_conservation()
    return result


def evaluate_independent_seed(
    matcher: PaymentMatcher,
    dataset: IndependentDataset,
    seed: int,
) -> BenchmarkSeedResult:
    """Evaluate clean-room independent dataset using only ground-truth fields."""
    result = BenchmarkSeedResult(
        seed=seed, generator_name="tests/adversarial/independent_generator.py"
    )
    result.total_payments = len(dataset.payments)

    tid = uuid.uuid4()
    uid = uuid.uuid4()
    now = datetime.now(timezone.utc)

    ret_map = {
        r.id: Retailer(
            id=r.id,
            tenant_id=tid,
            name=r.name_latin,
            phone=r.phone,
            bank_account=r.account_number,
            created_at=now,
        )
        for r in dataset.retailers
    }
    inv_map = {
        inv.id: Invoice(
            id=inv.id,
            tenant_id=tid,
            retailer_id=inv.retailer_id,
            invoice_number=inv.invoice_number,
            amount_santim=inv.amount_santim,
            issued_date=inv.issued_date,
            due_date=inv.due_date,
            state=InvoiceState.OPEN.value,
            created_at=now,
            created_by=uid,
        )
        for inv in dataset.invoices
    }

    invoices_list = list(inv_map.values())
    retailers_list = list(ret_map.values())

    for sp in dataset.payments:
        ntype = sp.noise_type
        if ntype not in result.noise_breakdown:
            result.noise_breakdown[ntype] = NoiseCategoryMetric(noise_type=ntype)
        nm = result.noise_breakdown[ntype]
        nm.total += 1

        exp_ids = set(sp.expected_invoice_ids)
        pl = PaymentLine(
            id=sp.id,
            tenant_id=tid,
            amount_santim=sp.amount_santim,
            value_date=sp.value_date,
            counterparty_name=sp.counterparty_name,
            counterparty_account=sp.counterparty_account,
            raw_reference=sp.raw_reference,
            evidence_level=sp.evidence_level,
            state=PaymentState.UNMATCHED.value,
            created_at=now,
        )
        res = matcher.match_payment(pl, invoices_list, retailers_list)
        cand_ids = [m.invoice_id for m in res.matches]

        if not exp_ids:
            result.spurious_payments += 1
            nm.spurious += 1
            if res.status == "auto_confirmed":
                result.false_confirmations += 1
                nm.false_confirmations += 1
            continue

        result.payments_with_true_invoice += 1
        nm.with_true_invoice += 1

        is_eligible = (
            sp.expected_status == "auto_confirmed"
            and not sp.is_partial
            and not sp.is_combined
            and not sp.is_ambiguous
            and sp.evidence_level in ("L1", "L2", "L3")
        )

        if res.status == "auto_confirmed":
            if is_eligible and set(cand_ids) == exp_ids and sp.evidence_level != "L4":
                result.auto_confirmed_correctly += 1
                nm.auto_confirmed_correctly += 1
            else:
                result.false_confirmations += 1
                nm.false_confirmations += 1
        elif res.status == "proposed":
            if cand_ids and cand_ids[0] in exp_ids:
                result.queued_top1 += 1
                nm.queued_top1 += 1
            elif any(cid in exp_ids for cid in cand_ids[1:3]):
                result.queued_top3 += 1
                nm.queued_top3 += 1
            elif any(cid in exp_ids for cid in cand_ids[3:]):
                result.queued_not_in_top3 += 1
                nm.queued_not_in_top3 += 1
            else:
                result.no_candidate_at_all += 1
                nm.no_candidate_at_all += 1
        else:  # unmatched
            result.no_candidate_at_all += 1
            nm.no_candidate_at_all += 1

    result.verify_conservation()
    return result


def print_benchmark_table(results: Sequence[BenchmarkSeedResult], generator_name: str) -> None:
    """Print complete formatted benchmark table across all 10 seeds."""
    print(f"\n{'=' * 115}")
    print(f"BENCHMARK REPORT: {generator_name}")
    print(f"{'=' * 115}")
    header = (
        f"{'Seed':>5} | {'Total':>5} | {'TrueInv':>7} | {'AutoOK':>6} | {'FalseConf':>9} | "
        f"{'QTop1':>5} | {'QTop3':>5} | {'QNot3':>5} | {'NoCand':>6} | {'Spurious':>8} | "
        f"{'Coverage':>8} | {'Top-1 Rate':>10}"
    )
    print(header)
    print(f"{'-' * 115}")

    for r in results:
        print(
            f"{r.seed:>5} | {r.total_payments:>5} | {r.payments_with_true_invoice:>7} | "
            f"{r.auto_confirmed_correctly:>6} | {r.false_confirmations:>9} | "
            f"{r.queued_top1:>5} | {r.queued_top3:>5} | {r.queued_not_in_top3:>5} | "
            f"{r.no_candidate_at_all:>6} | {r.spurious_payments:>8} | "
            f"{r.coverage:>8.1%} | {r.top1_rate:>10.1%}"
        )
    print(f"{'=' * 115}\n")
