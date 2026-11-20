"""
app/api/v1/formatting.py

Pure helpers for the /api/v1 response boundary.  Deliberately free of FastAPI
and SQLAlchemy imports so they can be unit-tested without a database.

Rule 1: money is stored as integer santim.  ETB floats exist ONLY in API
responses (``santim_to_etb``); inbound ETB amounts are converted with Decimal
(``etb_to_santim``) and never rounded silently.

Rule 2: ``verification_for`` is derived from the evidence level alone and fails
closed — anything that is not a confirmable level (L1/L2/L3), including
unknown values, is "unverified".  L4 can therefore never be reported verified.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from app.kernel.enums import CONFIRMABLE_EVIDENCE_LEVELS, EvidenceLevel

NO_SALES_LABEL = "No sales yet"


# ---------------------------------------------------------------------------
# Money (Rule 1)
# ---------------------------------------------------------------------------
def santim_to_etb(santim: int) -> float:
    """Convert integer santim to an ETB float. API response boundary only."""
    return int(santim) / 100.0


def etb_to_santim(amount: float | int | str) -> int:
    """
    Convert an inbound ETB amount to integer santim without float arithmetic.

    Raises ValueError for non-finite, non-positive, or sub-santim precision
    amounts (e.g. 0.285) instead of silently rounding money.
    """
    try:
        dec = Decimal(str(amount))
    except InvalidOperation as exc:
        raise ValueError("Amount is not a valid number.") from exc
    if not dec.is_finite():
        raise ValueError("Amount must be a finite number.")
    scaled = dec * 100
    if scaled != scaled.to_integral_value():
        raise ValueError("Amount must have at most two decimal places.")
    santim = int(scaled)
    if santim <= 0:
        raise ValueError("Amount must be greater than zero.")
    return santim


# ---------------------------------------------------------------------------
# Evidence ladder (Rule 2)
# ---------------------------------------------------------------------------
def verification_for(evidence_level: str | None) -> str:
    """'verified' only for L1/L2/L3; everything else (L4, unknown, None) is 'unverified'."""
    try:
        level = EvidenceLevel(evidence_level)
    except ValueError:
        return "unverified"
    return "verified" if level in CONFIRMABLE_EVIDENCE_LEVELS else "unverified"


def receipt_photo_on_file(
    evidence_level: str | None,
    raw_reference: str | None,
    source_type: str | None,
) -> bool:
    """
    True for every L4 entry (receipt photos and cash notes are always flagged),
    and for any line whose reference or import source mentions a receipt.
    This flag is informational only — it never influences ``verification``.
    """
    if evidence_level == EvidenceLevel.L4.value:
        return True
    haystack = f"{raw_reference or ''} {source_type or ''}".lower()
    return "receipt" in haystack


def describe_source(
    evidence_level: str | None,
    raw_reference: str | None,
    source_type: str | None,
) -> str:
    """Short human-readable provenance label for a review-queue row."""
    src = (source_type or "").lower()
    ref = (raw_reference or "").lower()
    if evidence_level == EvidenceLevel.L1.value:
        return "Gateway payment notification"
    if evidence_level == EvidenceLevel.L2.value:
        return "Telebirr statement line" if "telebirr" in src else "Bank statement line"
    if evidence_level == EvidenceLevel.L3.value:
        return "Buyer confirmation"
    if evidence_level == EvidenceLevel.L4.value:
        if "receipt" in src or "receipt" in ref or "photo" in src:
            return "Receipt photo only"
        return "Cash note only"
    return "Unverified entry"


# ---------------------------------------------------------------------------
# Dates (all UTC, matching the dashboard's "today")
# ---------------------------------------------------------------------------
def relative_day(day: date | None, today: date) -> str:
    if day is None:
        return NO_SALES_LABEL
    delta = (today - day).days
    if delta <= 0:
        return "today"
    if delta == 1:
        return "yesterday"
    return f"{delta} days ago"


def format_timestamp(moment: datetime, now: datetime) -> str:
    """'Today, 09:18' / 'Yesterday, 17:42' / '14 Nov, 09:18' (UTC)."""
    moment_utc = moment.astimezone(timezone.utc) if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    now_utc = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    delta_days = (now_utc.date() - moment_utc.date()).days
    clock = moment_utc.strftime("%H:%M")
    if delta_days == 0:
        return f"Today, {clock}"
    if delta_days == 1:
        return f"Yesterday, {clock}"
    return f"{moment_utc.day} {moment_utc.strftime('%b')}, {clock}"
