"""
app/kernel/receipt_verifier.py

DESIGN ONLY — NOT BUILT
=======================
Interface and null implementation for electronic receipt verification.
Strictly decoupled from core matching logic:
- Zero network I/O
- Always returns status='pending' by default
- No auto-confirmation can ever be produced from receipt verification

Placement in Evidence Ladder (Design Specification):
Ranks below an official bank statement line (L2) and above an unverified
photo (L4). Never auto-confirms. All produced matches are strictly 'proposed'.
Note: In accordance with project safety constraints, no new enum variant
is added to EvidenceLevel in app/kernel/enums.py.
"""

from __future__ import annotations

import dataclasses
from typing import Protocol, runtime_checkable


@dataclasses.dataclass(frozen=True)
class VerificationResult:
    """
    Immutable result of a receipt verification attempt.
    Status can be: 'pending' | 'verified' | 'failed' | 'rejected'
    """
    status: str
    verified: bool
    reason: str
    provider_name: str | None = None
    transaction_reference: str | None = None


@runtime_checkable
class ReceiptVerifier(Protocol):
    """
    Protocol for external receipt verification providers (Telebirr, CBE, etc.).
    Implementations must be fail-closed: any network issue or uncertainty
    must resolve to status='pending'.
    """

    def verify(
        self,
        receipt_reference: str,
        expected_amount_santim: int,
        merchant_account: str,
    ) -> VerificationResult:
        """
        Verify a receipt reference against the issuing provider.
        Must never perform synchronous blocking network calls on the main thread.
        """
        ...


class NullVerifier:
    """
    Default null verifier. Disabled by default.
    Always returns status='pending' with verified=False.
    Guarantees zero network calls and safe offline behavior.
    """

    def __init__(self, *, provider_name: str = "null_provider") -> None:
        self.provider_name = provider_name

    def verify(
        self,
        receipt_reference: str,
        expected_amount_santim: int,
        merchant_account: str,
    ) -> VerificationResult:
        """
        Always return pending without network access.
        """
        return VerificationResult(
            status="pending",
            verified=False,
            reason="Receipt verification disabled by default (Design only)",
            provider_name=self.provider_name,
            transaction_reference=receipt_reference.strip() if receipt_reference else None,
        )
