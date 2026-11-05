"""
app/matching/evaluator.py

Evaluation engine for measuring matcher accuracy, precision, recall, review-queue
share, and verifying the non-negotiable target of EXACTLY ZERO false confirmations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.matching.matcher import MatchResult, PaymentMatcher
from app.matching.synthetic import SyntheticDataSet, SyntheticPaymentGroundTruth


@dataclass
class NoiseTypeMetric:
    noise_type: str
    total_count: int = 0
    auto_confirmed_count: int = 0
    proposed_count: int = 0
    unmatched_count: int = 0
    false_confirmations: int = 0
    correct_matches: int = 0


@dataclass
class MatchingEvaluationReport:
    total_payments: int = 0
    total_eligible_auto: int = 0
    system_auto_confirmed: int = 0
    system_proposed: int = 0
    system_unmatched: int = 0

    true_auto_confirmed: int = 0
    false_confirmations: int = 0  # TARGET: EXACTLY ZERO

    auto_match_precision: float = 0.0  # (True Auto) / (System Auto)
    auto_match_recall: float = 0.0     # (True Auto) / (Eligible Auto) -> TARGET: >= 90%
    review_queue_share: float = 0.0    # (System Proposed) / (Total Payments)

    noise_metrics: dict[str, NoiseTypeMetric] = field(default_factory=dict)
    detailed_errors: list[dict[str, Any]] = field(default_factory=list)

    def is_target_met(self) -> bool:
        """Target: at least 90% correct auto-matches AND exactly ZERO false confirmations."""
        return self.false_confirmations == 0 and self.auto_match_recall >= 0.90

    def generate_summary_text(self) -> str:
        lines = [
            "=" * 70,
            "DAILY CLOSE — PAYMENT MATCHING ACCURACY REPORT",
            "=" * 70,
            f"Total Processed Payments : {self.total_payments}",
            f"Eligible Auto-Confirmable: {self.total_eligible_auto}",
            f"System Auto-Confirmed   : {self.system_auto_confirmed}",
            f"System Review Queue     : {self.system_proposed} ({self.review_queue_share * 100:.1f}% share)",
            f"System Unmatched        : {self.system_unmatched}",
            "-" * 70,
            f"True Auto-Confirmations : {self.true_auto_confirmed}",
            f"FALSE CONFIRMATIONS     : {self.false_confirmations} (Target: EXACTLY 0)",
            f"Auto-Match Precision    : {self.auto_match_precision * 100:.2f}%",
            f"Auto-Match Recall       : {self.auto_match_recall * 100:.2f}% (Target: >= 90.0%)",
            "-" * 70,
            "PERFORMANCE BREAKDOWN BY NOISE CATEGORY:",
            f"{'Category':<24} | {'Total':<6} | {'Auto':<6} | {'Proposed':<8} | {'False Conf':<10}",
            "-" * 70,
        ]

        for ntype, metric in sorted(self.noise_metrics.items()):
            lines.append(
                f"{ntype:<24} | {metric.total_count:<6} | {metric.auto_confirmed_count:<6} | "
                f"{metric.proposed_count:<8} | {metric.false_confirmations:<10}"
            )

        lines.extend([
            "=" * 70,
            f"TARGET STATUS: {'PASSED [OK]' if self.is_target_met() else 'FAILED [MISSED TARGET]'}",
            "=" * 70,
        ])
        return "\n".join(lines)


def evaluate_matcher(
    matcher: PaymentMatcher,
    dataset: SyntheticDataSet,
) -> MatchingEvaluationReport:
    """
    Run matcher on synthetic dataset and generate performance metrics against ground truth.
    """
    report = MatchingEvaluationReport()
    report.total_payments = len(dataset.payments_with_truth)

    for truth in dataset.payments_with_truth:
        ntype = truth.noise_type
        if ntype not in report.noise_metrics:
            report.noise_metrics[ntype] = NoiseTypeMetric(noise_type=ntype)
        nm = report.noise_metrics[ntype]
        nm.total_count += 1

        if truth.is_auto_confirmable:
            report.total_eligible_auto += 1

        # Match payment against open invoices
        result: MatchResult = matcher.match_payment(
            truth.payment_line,
            dataset.invoices,
            dataset.retailers,
        )

        matched_inv_ids = [m.invoice_id for m in result.matches]

        if result.status == "auto_confirmed":
            report.system_auto_confirmed += 1
            nm.auto_confirmed_count += 1

            # Verification for False Confirmation
            is_false = False
            error_reason = ""

            # Check 1: Was it eligible to be auto-confirmed?
            if not truth.is_auto_confirmable:
                is_false = True
                error_reason = f"Payment was not auto-confirmable (type={ntype})"
            # Check 2: Did it match the correct expected invoice?
            elif set(matched_inv_ids) != set(truth.expected_invoice_ids):
                is_false = True
                error_reason = f"Matched wrong invoice! Expected {truth.expected_invoice_ids}, got {matched_inv_ids}"
            # Check 3: Rule 2 check — evidence level
            elif truth.payment_line.evidence_level == "L4":
                is_false = True
                error_reason = "CRITICAL: Auto-confirmed on L4 evidence alone!"

            if is_false:
                report.false_confirmations += 1
                nm.false_confirmations += 1
                report.detailed_errors.append({
                    "payment_id": str(truth.payment_line.id),
                    "noise_type": ntype,
                    "reason": error_reason,
                    "result_reason": result.reason,
                })
            else:
                report.true_auto_confirmed += 1
                nm.correct_matches += 1

        elif result.status == "proposed":
            report.system_proposed += 1
            nm.proposed_count += 1
            # If ground truth expected proposed, this is a correct handling!
            if truth.expected_status == "proposed":
                nm.correct_matches += 1

        else:  # unmatched
            report.system_unmatched += 1
            nm.unmatched_count += 1
            if truth.expected_status == "unmatched":
                nm.correct_matches += 1

    # Calculate overall metrics
    if report.system_auto_confirmed > 0:
        report.auto_match_precision = report.true_auto_confirmed / report.system_auto_confirmed
    else:
        report.auto_match_precision = 1.0

    if report.total_eligible_auto > 0:
        report.auto_match_recall = report.true_auto_confirmed / report.total_eligible_auto
    else:
        report.auto_match_recall = 0.0

    if report.total_payments > 0:
        report.review_queue_share = report.system_proposed / report.total_payments

    return report
