"""
app/kernel/enums.py

All domain enumerations.  These are the single source of truth for state
machines and evidence levels.  Import them everywhere; never use bare strings.
"""

from enum import Enum


class EvidenceLevel(str, Enum):
    """
    Evidence hierarchy for payment records.

    Only L1, L2, L3 can advance a payment to CONFIRMED.
    L4 can only produce a PROPOSED match; an owner may escalate to
    ACCEPTED_UNVERIFIED, which is NEVER counted as confirmed paid.
    """

    L1 = "L1"  # Signature-verified licensed-gateway webhook
    L2 = "L2"  # Line from an imported bank or Telebirr statement
    L3 = "L3"  # Retailer confirmation via a signed, expiring link
    L4 = "L4"  # Screenshot, forwarded SMS, or manual note


class PaymentState(str, Enum):
    """
    State machine for a payment_line record.

    Allowed transitions (enforced in app/kernel/guards.py):
        unmatched -> proposed        (any evidence level)
        proposed  -> confirmed       (L1 / L2 / L3 only)
        proposed  -> accepted_unverified  (L4; owner-only override)
        proposed  -> rejected        (any actor with permission)
        confirmed -> rejected        (compensating event only)

    ACCEPTED_UNVERIFIED is intentionally distinct from CONFIRMED and is
    excluded from all totals, reports, and exports that count as "paid".
    """

    UNMATCHED = "unmatched"
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    ACCEPTED_UNVERIFIED = "accepted_unverified"  # L4 owner override; NEVER = paid
    REJECTED = "rejected"


class InvoiceState(str, Enum):
    OPEN = "open"
    PARTIALLY_PAID = "partially_paid"
    SETTLED = "settled"
    DISPUTED = "disputed"
    VOID = "void"


class MatchState(str, Enum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class MatchTier(str, Enum):
    """
    Matching tier that produced a payment_match row.

    TIER_A: invoice number found in reference AND amounts equal.
    TIER_B: counterparty phone/account + amount + date within window.
    TIER_C: fuzzy retailer name + amount.
    """

    TIER_A = "tier_a"
    TIER_B = "tier_b"
    TIER_C = "tier_c"


class UserRole(str, Enum):
    OWNER = "owner"
    CASHIER = "cashier"


# ---------------------------------------------------------------------------
# Evidence levels that are permitted to CONFIRM a payment.
# This set is the authoritative source used by guards.py.
# ---------------------------------------------------------------------------
CONFIRMABLE_EVIDENCE_LEVELS: frozenset[EvidenceLevel] = frozenset(
    {EvidenceLevel.L1, EvidenceLevel.L2, EvidenceLevel.L3}
)
