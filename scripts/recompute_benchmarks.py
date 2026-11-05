"""
scripts/recompute_benchmarks.py

Recomputes every benchmark summary statistic from raw execution results
across 10 random seeds on both the primary and clean-room independent generators.
Regenerates benchmark numbers for final-delivery-report.md.
"""

from __future__ import annotations

import os
import sys

# Ensure repo root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.matching.matcher import PaymentMatcher
from app.matching.synthetic import generate_synthetic_dataset
from tests.adversarial.benchmark_metrics import (
    BENCHMARK_SEEDS,
    BenchmarkSeedResult,
    evaluate_independent_seed,
    evaluate_synthetic_seed,
)
from tests.adversarial.independent_generator import IndependentSyntheticGenerator


def format_table(results: list[BenchmarkSeedResult], generator_title: str) -> str:
    lines = []
    lines.append("=" * 115)
    lines.append(f"BENCHMARK REPORT: {generator_title}")
    lines.append("=" * 115)
    lines.append(
        f"{'Seed':>5} | {'Total':>5} | {'TrueInv':>7} | {'AutoOK':>6} | {'FalseConf':>9} | "
        f"{'QTop1':>5} | {'QTop3':>5} | {'QNot3':>5} | {'NoCand':>6} | {'Spurious':>8} | "
        f"{'Coverage':>8} | {'Top-1 Rate':>10}"
    )
    lines.append("-" * 115)

    for r in results:
        lines.append(
            f"{r.seed:>5} | {r.total_payments:>5} | {r.payments_with_true_invoice:>7} | "
            f"{r.auto_confirmed_correctly:>6} | {r.false_confirmations:>9} | "
            f"{r.queued_top1:>5} | {r.queued_top3:>5} | {r.queued_not_in_top3:>5} | "
            f"{r.no_candidate_at_all:>6} | {r.spurious_payments:>8} | "
            f"{r.coverage:>8.1%} | {r.top1_rate:>10.1%}"
        )
    lines.append("=" * 115)
    return "\n".join(lines)


def run_benchmarks() -> tuple[list[BenchmarkSeedResult], list[BenchmarkSeedResult]]:
    matcher = PaymentMatcher()

    print("\n[1/2] Computing Table 1: app/matching/synthetic.py (10 Seeds)...")
    synth_results = []
    for s in BENCHMARK_SEEDS:
        ds = generate_synthetic_dataset(seed=s)
        res = evaluate_synthetic_seed(matcher, ds, seed=s)
        res.verify_conservation()
        synth_results.append(res)

    print("[2/2] Computing Table 2: tests/adversarial/independent_generator.py (10 Seeds)...")
    indep_results = []
    for s in BENCHMARK_SEEDS:
        gen = IndependentSyntheticGenerator(seed=s)
        ds = gen.generate(num_retailers=25, num_invoices=100, num_payments=150)
        res = evaluate_independent_seed(matcher, ds, seed=s)
        res.verify_conservation()
        indep_results.append(res)

    return synth_results, indep_results


def main() -> int:
    synth_res, indep_res = run_benchmarks()

    table1 = format_table(synth_res, "app/matching/synthetic.py (10 Seeds)")
    table2 = format_table(indep_res, "tests/adversarial/independent_generator.py (10 Seeds)")

    print("\n" + table1 + "\n")
    print("\n" + table2 + "\n")

    # Aggregate summaries
    print("Aggregate Summary Statistics:")
    print("Synthetic Generator (10 seeds):")
    print(f"  Total False Confirmations: {sum(r.false_confirmations for r in synth_res)}")
    print(f"  Mean Coverage:             {sum(r.coverage for r in synth_res) / len(synth_res):.2%}")
    print(f"  Mean Top-1 Rate:           {sum(r.top1_rate for r in synth_res) / len(synth_res):.2%}")

    print("Independent Clean-Room Generator (10 seeds):")
    print(f"  Total False Confirmations: {sum(r.false_confirmations for r in indep_res)}")
    print(f"  Mean Coverage:             {sum(r.coverage for r in indep_res) / len(indep_res):.2%}")
    print(f"  Mean Top-1 Rate:           {sum(r.top1_rate for r in indep_res) / len(indep_res):.2%}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
