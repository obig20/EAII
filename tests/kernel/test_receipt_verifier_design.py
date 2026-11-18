"""
tests/kernel/test_receipt_verifier_design.py

Stage 4: Receipt Verification — Design Only Tests
Verifies the NullVerifier interface, protocol compliance, zero network I/O,
and invariant that EvidenceLevel enum is completely untouched.

ALL SAMPLES USED ARE STRICTLY SYNTHETIC.
"""

from __future__ import annotations

import socket
import pytest

from app.kernel.enums import EvidenceLevel
from app.kernel.receipt_verifier import (
    NullVerifier,
    ReceiptVerifier,
    VerificationResult,
)


class TestReceiptVerifierDesign:
    """Verifies interface properties of the Stage 4 receipt verification design."""

    def test_null_verifier_implements_protocol(self):
        """NullVerifier must satisfy the ReceiptVerifier runtime protocol."""
        verifier = NullVerifier()
        assert isinstance(verifier, ReceiptVerifier), \
            "NullVerifier must implement ReceiptVerifier protocol"

    def test_null_verifier_always_pending(self):
        """NullVerifier must always return status='pending' and verified=False."""
        verifier = NullVerifier()
        # Synthetic test inputs
        result = verifier.verify(
            receipt_reference="SYNTH-CBE-TXN-12345",
            expected_amount_santim=250_000,
            merchant_account="1000123456789",
        )
        assert isinstance(result, VerificationResult)
        assert result.status == "pending"
        assert result.verified is False
        assert "disabled by default" in result.reason.lower()
        assert result.transaction_reference == "SYNTH-CBE-TXN-12345"

    def test_null_verifier_zero_network_calls(self, monkeypatch):
        """Verification must never open socket connections or make HTTP requests."""
        def guarded_connect(*args, **kwargs):
            raise RuntimeError("CRITICAL: Network call attempted in NullVerifier!")

        monkeypatch.setattr(socket.socket, "connect", guarded_connect)

        verifier = NullVerifier()
        # Must execute cleanly with socket connect blocked
        result = verifier.verify(
            receipt_reference="SYNTH-TELEBIRR-98765",
            expected_amount_santim=50_000,
            merchant_account="0911000000",
        )
        assert result.status == "pending"

    def test_evidence_level_enum_is_untouched(self):
        """
        SAFETY RULE: No new evidence level must be added to app/kernel/enums.py.
        EvidenceLevel enum must contain only L1, L2, L3, L4.
        """
        levels = [e.value for e in EvidenceLevel]
        expected_levels = ["L1", "L2", "L3", "L4"]
        assert sorted(levels) == sorted(expected_levels), \
            f"EvidenceLevel enum was illegally modified! Found: {levels}, expected: {expected_levels}"

    def test_verification_result_is_immutable(self):
        """VerificationResult dataclass must be frozen to prevent tampering."""
        result = VerificationResult(
            status="pending",
            verified=False,
            reason="Design only test",
        )
        with pytest.raises(Exception):
            result.status = "verified"  # type: ignore[misc]
