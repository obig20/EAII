"""
tests/matching/test_synthetic_accuracy.py

Synthetic benchmark test:
  - 30 retailers
  - 300 invoices
  - 400 payment lines with injected noise
Targets to verify:
  1. EXACTLY ZERO false confirmations (false_confirmations == 0).
  2. At least 90% correct auto-matches (auto_match_recall >= 0.90).
"""

from app.matching.evaluator import evaluate_matcher
from app.matching.matcher import PaymentMatcher
from app.matching.synthetic import generate_synthetic_dataset


class TestSyntheticMatchingAccuracy:
    def test_synthetic_accuracy_targets(self, capsys):
        """
        Run the deterministic synthetic benchmark and verify targets:
          - Precision: 100.0% (EXACTLY ZERO false confirmations)
          - Recall: >= 90.0%
        """
        dataset = generate_synthetic_dataset(seed=42)

        assert len(dataset.retailers) == 30, "Must have exactly 30 retailers"
        assert len(dataset.invoices) >= 300, "Must have at least 300 invoices"
        assert len(dataset.payments_with_truth) == 400, "Must have exactly 400 payment lines"

        matcher = PaymentMatcher()
        report = evaluate_matcher(matcher, dataset)

        # Print detailed report into test output
        summary = report.generate_summary_text()
        print("\n" + summary)

        # Non-negotiable Target 1: EXACTLY ZERO false confirmations
        assert report.false_confirmations == 0, (
            f"NON-NEGOTIABLE VIOLATION: Encountered {report.false_confirmations} false confirmations! "
            f"Errors: {report.detailed_errors}"
        )

        # Non-negotiable Target 2: At least 90% correct auto-matches
        assert report.auto_match_recall >= 0.90, (
            f"TARGET MISSED: Auto-match recall was {report.auto_match_recall * 100:.2f}% "
            f"(target: >= 90.0%). True auto: {report.true_auto_confirmed}/{report.total_eligible_auto}"
        )

        # Non-negotiable Target 3: Precision must be 100%
        assert report.auto_match_precision == 1.0, (
            f"Auto-match precision must be 100% (got {report.auto_match_precision * 100:.2f}%)"
        )
