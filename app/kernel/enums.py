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
    REVERSED = "reversed"


class ReversalReasonCode(str, Enum):
    """
    Structured reason code for payment match reversal.
    Stored in append-only event payloads without personal data.
    """
    WRONG_INVOICE = "wrong_invoice"
    WRONG_AMOUNT = "wrong_amount"
    DUPLICATE = "duplicate"
    BANK_REVERSAL = "bank_reversal"
    OTHER = "other"


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


class StockMovementType(str, Enum):
    RECEIPT = "receipt"
    DISPATCH_SALE = "dispatch_sale"
    TRANSFER_IN = "transfer_in"
    TRANSFER_OUT = "transfer_out"
    DAMAGE_SHRINKAGE = "damage_shrinkage"
    ADJUSTMENT = "adjustment"


class PurchaseOrderState(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    PARTIALLY_RECEIVED = "partially_received"
    FULFILLED = "fulfilled"
    CANCELLED = "cancelled"


class PurchaseInvoiceState(str, Enum):
    OPEN = "open"
    PARTIALLY_PAID = "partially_paid"
    SETTLED = "settled"
    DISPUTED = "disputed"


class DeliveryTripState(str, Enum):
    PLANNED = "planned"
    DISPATCHED = "dispatched"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class DeliveryStopStatus(str, Enum):
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED_RETURNED = "failed_returned"


class CashHandoverStatus(str, Enum):
    PENDING = "pending"
    VERIFIED_MATCH = "verified_match"
    VERIFIED_DISCREPANCY = "verified_discrepancy"
    REJECTED = "rejected"


class TaxRateType(str, Enum):
    VAT_15 = "vat_15"          # 15% standard Ethiopian VAT
    WITHHOLDING_2 = "withholding_2"  # 2% withholding on purchases >= 10,000 ETB
    TOT_10 = "tot_10"          # 10% Turnover Tax
    EXEMPT = "exempt"          # 0% tax exempt


class AccountCategory(str, Enum):
    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"
    REVENUE = "revenue"
    EXPENSE = "expense"


class JournalEntryState(str, Enum):
    POSTED = "posted"
    VOID = "void"


# ---------------------------------------------------------------------------
# Evidence levels that are permitted to CONFIRM a payment.
# This set is the authoritative source used by guards.py.
# ---------------------------------------------------------------------------
CONFIRMABLE_EVIDENCE_LEVELS: frozenset[EvidenceLevel] = frozenset(
    {EvidenceLevel.L1, EvidenceLevel.L2, EvidenceLevel.L3}
)
