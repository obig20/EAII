"""
tests/adversarial/test_benchmark_audit.py

Benchmark Audit across 10 random seeds:
1. Multi-seed evaluation of app/matching/synthetic.py across 10 random seeds
   Seeds: [1, 42, 100, 314, 777, 1024, 2026, 4096, 8192, 9999]
2. Clean-room evaluation on independent generator (tests/adversarial/independent_generator.py)
   across the same 10 seeds.
3. Verification of:
   - Exactly ZERO false confirmations on all seeds
   - Coverage (auto-correct + true invoice in top 3) >= 95.0%
   - Top-1 proposal accuracy >= 85.0% of queued payments
   - Conservation of counts
   - Per-noise-type breakdown
"""

from __future__ import annotations

import pytest

from app.matching.matcher import PaymentMatcher
from app.matching.synthetic import generate_synthetic_dataset
from tests.adversarial.benchmark_metrics import (
    BENCHMARK_SEEDS,
    evaluate_independent_seed,
    evaluate_synthetic_seed,
    print_benchmark_table,
)
from tests.adversarial.independent_generator import IndependentSyntheticGenerator


class TestBenchmarkAuditing:
    @pytest.mark.slow
    def test_synthetic_benchmark_10_seeds(self):
        """Run benchmark on default synthetic generator across 10 random seeds."""
        matcher = PaymentMatcher()
        results = []

        for s in BENCHMARK_SEEDS:
            dataset = generate_synthetic_dataset(seed=s)
            res = evaluate_synthetic_seed(matcher, dataset, seed=s)
            results.append(res)

        print_benchmark_table(results, "app/matching/synthetic.py (10 Seeds)")

        # Verify acceptance criteria across all seeds
        for res in results:
            assert res.false_confirmations == 0, (
                f"Seed {res.seed}: False confirmations detected ({res.false_confirmations})!"
            )
            assert res.coverage >= 0.95, (
                f"Seed {res.seed}: Coverage was {res.coverage:.2%} (target: >= 95.0%)"
            )
            assert res.top1_rate >= 0.85, (
                f"Seed {res.seed}: Top-1 accuracy was {res.top1_rate:.2%} (target: >= 85.0%)"
            )

    @pytest.mark.slow
    def test_independent_generator_10_seeds(self):
        """Run benchmark on clean-room independent generator across 10 random seeds."""
        matcher = PaymentMatcher()
        results = []

        for s in BENCHMARK_SEEDS:
            gen = IndependentSyntheticGenerator(seed=s)
            dataset = gen.generate(num_retailers=25, num_invoices=100, num_payments=150)
            res = evaluate_independent_seed(matcher, dataset, seed=s)
            results.append(res)

        print_benchmark_table(results, "tests/adversarial/independent_generator.py (10 Seeds)")

        # Verify acceptance criteria across all seeds
        for res in results:
            assert res.false_confirmations == 0, (
                f"Seed {res.seed}: False confirmations detected ({res.false_confirmations})!"
            )
            assert res.coverage >= 0.95, (
                f"Seed {res.seed}: Coverage was {res.coverage:.2%} (target: >= 95.0%)"
            )
            assert res.top1_rate >= 0.85, (
                f"Seed {res.seed}: Top-1 accuracy was {res.top1_rate:.2%} (target: >= 85.0%)"
            )
