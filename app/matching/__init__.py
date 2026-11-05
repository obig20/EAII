"""app/matching package"""

from app.matching.evaluator import MatchingEvaluationReport, evaluate_matcher
from app.matching.matcher import MatchItem, MatchResult, MatcherConfig, PaymentMatcher
from app.matching.normalization import (
    normalize_account_number,
    normalize_ethiopian_name,
    normalize_phone,
    string_similarity,
)
from app.matching.synthetic import (
    SyntheticDataSet,
    SyntheticPaymentGroundTruth,
    generate_synthetic_dataset,
)

__all__ = [
    "PaymentMatcher",
    "MatcherConfig",
    "MatchResult",
    "MatchItem",
    "normalize_phone",
    "normalize_account_number",
    "normalize_ethiopian_name",
    "string_similarity",
    "SyntheticDataSet",
    "SyntheticPaymentGroundTruth",
    "generate_synthetic_dataset",
    "evaluate_matcher",
    "MatchingEvaluationReport",
]
