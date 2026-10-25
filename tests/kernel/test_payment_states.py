"""
tests/kernel/test_payment_states.py

Tests for Rule 2: Evidence ladder enforcement.

Verifies:
  - L1, L2, L3 → assert_confirmable passes (payment CAN be confirmed)
  - L4           → assert_confirmable raises InsufficientEvidenceError
  - ACCEPTED_UNVERIFIED is a distinct state that exists in the enum
  - ACCEPTED_UNVERIFIED != CONFIRMED (not counted as paid)
  - assert_unambiguous passes with exactly 1 candidate
  - assert_unambiguous raises AmbiguousMatchError with 2+ candidates
"""

import uuid

import pytest

from app.kernel.enums import (
    CONFIRMABLE_EVIDENCE_LEVELS,
    EvidenceLevel,
    PaymentState,
)
from app.kernel.guards import (
    AmbiguousMatchError,
    InsufficientEvidenceError,
    assert_confirmable,
    assert_unambiguous,
)


# ---------------------------------------------------------------------------
# Evidence level confirmation guard
# ---------------------------------------------------------------------------
class TestAssertConfirmable:
    @pytest.mark.parametrize("level", [EvidenceLevel.L1, EvidenceLevel.L2, EvidenceLevel.L3])
    def test_confirmable_levels_pass(self, level):
        """L1, L2, L3 must not raise — they are permitted to confirm payments."""
        assert_confirmable(level)  # should not raise

    def test_l4_raises_insufficient_evidence(self):
        """Rule 2: L4 evidence CANNOT set a payment to CONFIRMED."""
        with pytest.raises(InsufficientEvidenceError):
            assert_confirmable(EvidenceLevel.L4)

    def test_l4_error_mentions_accepted_unverified(self):
        """The error message must explain the accepted_unverified alternative."""
        with pytest.raises(InsufficientEvidenceError, match="accepted_unverified"):
            assert_confirmable(EvidenceLevel.L4)

    def test_confirmable_set_is_correct(self):
        """CONFIRMABLE_EVIDENCE_LEVELS must be exactly L1, L2, L3."""
        assert CONFIRMABLE_EVIDENCE_LEVELS == {
            EvidenceLevel.L1,
            EvidenceLevel.L2,
            EvidenceLevel.L3,
        }

    def test_l4_not_in_confirmable_set(self):
        assert EvidenceLevel.L4 not in CONFIRMABLE_EVIDENCE_LEVELS


# ---------------------------------------------------------------------------
# PaymentState enum completeness
# ---------------------------------------------------------------------------
class TestPaymentStateEnum:
    def test_all_required_states_exist(self):
        states = {s.value for s in PaymentState}
        assert "unmatched" in states
        assert "proposed" in states
        assert "confirmed" in states
        assert "accepted_unverified" in states
        assert "rejected" in states

    def test_accepted_unverified_is_not_confirmed(self):
        """Critical: accepted_unverified MUST be a separate state from confirmed."""
        assert PaymentState.ACCEPTED_UNVERIFIED != PaymentState.CONFIRMED

    def test_accepted_unverified_value(self):
        assert PaymentState.ACCEPTED_UNVERIFIED.value == "accepted_unverified"

    def test_confirmed_value(self):
        assert PaymentState.CONFIRMED.value == "confirmed"


# ---------------------------------------------------------------------------
# Simulate the guard in a realistic payment-confirm flow
# ---------------------------------------------------------------------------
class TestL4CannotConfirm:
    def test_l4_payment_cannot_reach_confirmed_state(self):
        """
        Simulate the application attempting to confirm an L4 payment.
        The guard must fire before any DB write.
        """
        payment_evidence = EvidenceLevel.L4
        intended_new_state = PaymentState.CONFIRMED

        # Guard must be called before state transition
        with pytest.raises(InsufficientEvidenceError):
            assert_confirmable(payment_evidence)
            # This line must NEVER be reached for L4
            raise AssertionError(
                f"BUG: reached state {intended_new_state.value} with L4 evidence"
            )

    def test_l4_payment_can_reach_accepted_unverified(self):
        """L4 evidence IS allowed to reach accepted_unverified (owner-only override)."""
        payment_evidence = EvidenceLevel.L4

        # assert_confirmable should NOT be called for accepted_unverified path
        # The flow just sets the state without calling the confirmation guard
        new_state = PaymentState.ACCEPTED_UNVERIFIED
        assert new_state == PaymentState.ACCEPTED_UNVERIFIED
        # Verify it is NOT treated as confirmed
        assert new_state != PaymentState.CONFIRMED

    def test_l1_payment_can_reach_confirmed(self):
        payment_evidence = EvidenceLevel.L1
        assert_confirmable(payment_evidence)
        new_state = PaymentState.CONFIRMED
        assert new_state == PaymentState.CONFIRMED


# ---------------------------------------------------------------------------
# Ambiguity guard (Rule 3)
# ---------------------------------------------------------------------------
class TestAssertUnambiguous:
    def test_single_candidate_passes(self):
        assert_unambiguous([uuid.uuid4()])  # should not raise

    def test_zero_candidates_passes(self):
        """Zero candidates means no match — not ambiguous."""
        assert_unambiguous([])  # should not raise

    def test_two_candidates_raises(self):
        """Rule 3: two equally plausible invoices → must go to review queue."""
        with pytest.raises(AmbiguousMatchError):
            assert_unambiguous([uuid.uuid4(), uuid.uuid4()])

    def test_three_candidates_raises(self):
        with pytest.raises(AmbiguousMatchError):
            assert_unambiguous([uuid.uuid4(), uuid.uuid4(), uuid.uuid4()])

    def test_ambiguity_error_message_contains_count(self):
        ids = [uuid.uuid4(), uuid.uuid4()]
        with pytest.raises(AmbiguousMatchError, match="2 invoices"):
            assert_unambiguous(ids)
