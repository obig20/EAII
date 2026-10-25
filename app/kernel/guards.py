"""
app/kernel/guards.py

Deterministic state-transition guards for the payment evidence ladder.

Rules enforced here:
  Rule 2: Only L1, L2, L3 can set a payment to CONFIRMED.
           L4 can only produce PROPOSED or ACCEPTED_UNVERIFIED.
  Rule 3: Never auto-confirm when two or more invoices are equally plausible.

No LLM, no probabilistic logic — deterministic rule checks only.
"""

from __future__ import annotations

from app.kernel.enums import (
    CONFIRMABLE_EVIDENCE_LEVELS,
    EvidenceLevel,
    PaymentState,
)


# ---------------------------------------------------------------------------
# Evidence-level confirmation guard
# ---------------------------------------------------------------------------
class InsufficientEvidenceError(ValueError):
    """Raised when evidence level is too low to confirm a payment."""


def assert_confirmable(evidence_level: EvidenceLevel) -> None:
    """
    Assert that the given evidence level permits setting state to CONFIRMED.

    Raises:
        InsufficientEvidenceError: if evidence_level is L4 (or any future
            level below the confirmation threshold).
    """
    if evidence_level not in CONFIRMABLE_EVIDENCE_LEVELS:
        raise InsufficientEvidenceError(
            f"Evidence level {evidence_level.value!r} cannot set a payment to "
            f"{PaymentState.CONFIRMED.value!r}. "
            f"Confirmable levels: {sorted(e.value for e in CONFIRMABLE_EVIDENCE_LEVELS)}. "
            f"For L4 evidence, an owner may use "
            f"{PaymentState.ACCEPTED_UNVERIFIED.value!r} — this state is "
            f"NEVER counted as confirmed paid in any total or report."
        )


# ---------------------------------------------------------------------------
# Ambiguity guard (Rule 3)
# ---------------------------------------------------------------------------
class AmbiguousMatchError(ValueError):
    """
    Raised when two or more invoices are equally plausible for a payment.
    The payment must be sent to the review queue; auto-confirm is blocked.
    """


def assert_unambiguous(candidate_invoice_ids: list[object]) -> None:
    """
    Assert there is exactly one candidate invoice for auto-confirmation.

    Args:
        candidate_invoice_ids: List of invoice IDs that matched the payment.

    Raises:
        AmbiguousMatchError: if more than one candidate exists.
    """
    if len(candidate_invoice_ids) > 1:
        raise AmbiguousMatchError(
            f"Cannot auto-confirm: {len(candidate_invoice_ids)} invoices are "
            f"equally plausible ({candidate_invoice_ids}). "
            "Sending to review queue."
        )
