"""
tests/matching/test_synthetic_accuracy.py

Synthetic benchmark accuracy test:
  - 30 retailers
  - 300 invoices
  - 400 payment lines with injected noise
Targets to verify (approved acceptance criteria):
  1. EXACTLY ZERO false confirmations (false_confirmations == 0).
  2. Coverage (auto-correct + true invoice in top 3) >= 95.0% of payments with a true invoice.
  3. Top-1 proposal accuracy >= 85.0% of queued payments.
"""

import pytest

from app.matching.matcher import PaymentMatcher
from app.matching.synthetic import generate_synthetic_dataset
from tests.adversarial.benchmark_metrics import evaluate_synthetic_seed, print_benchmark_table


class TestSyntheticMatchingAccuracy:
    @pytest.mark.slow
    def test_synthetic_accuracy_targets(self, capsys):
        """
        Run the deterministic synthetic benchmark (seed=42) and verify acceptance targets:
          - Zero false confirmations
          - Coverage >= 95.0%
          - Top-1 proposal accuracy >= 85.0%
          - Strict conservation of counts
        """
        dataset = generate_synthetic_dataset(seed=42)

        assert len(dataset.retailers) == 30, "Must have exactly 30 retailers"
        assert len(dataset.invoices) >= 300, "Must have at least 300 invoices"
        assert len(dataset.payments_with_truth) == 400, "Must have exactly 400 payment lines"

        matcher = PaymentMatcher()
        result = evaluate_synthetic_seed(matcher, dataset, seed=42)

        # Print benchmark table and noise breakdown
        print_benchmark_table([result], "app/matching/synthetic.py (Seed 42)")

        # Target 1: EXACTLY ZERO false confirmations
        assert result.false_confirmations == 0, (
            f"NON-NEGOTIABLE VIOLATION: Encountered {result.false_confirmations} false confirmations!"
        )

        # Target 2: Coverage >= 95.0%
        assert result.coverage >= 0.95, (
            f"TARGET MISSED: Coverage was {result.coverage:.2%} (target: >= 95.0%). "
            f"Auto: {result.auto_confirmed_correctly}, Top-1: {result.queued_top1}, Top-3: {result.queued_top3}, True: {result.payments_with_true_invoice}"
        )

        # Target 3: Top-1 accuracy >= 85.0% of queued payments
        assert result.top1_rate >= 0.85, (
            f"TARGET MISSED: Top-1 proposal accuracy was {result.top1_rate:.2%} (target: >= 85.0%). "
            f"Top-1: {result.queued_top1}, Total queued: {result.queued_total}"
        )
