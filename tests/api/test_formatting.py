"""
tests/api/test_formatting.py

Pure unit tests for the /api/v1 response-boundary helpers (no database needed).
Covers Rule 1 (integer santim in, ETB float out, no silent rounding) and
Rule 2 (verification derives from evidence level and fails closed).
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.api.v1.formatting import (
    NO_SALES_LABEL,
    describe_source,
    etb_to_santim,
    format_timestamp,
    receipt_photo_on_file,
    relative_day,
    santim_to_etb,
    verification_for,
)


class TestMoney:
    def test_santim_to_etb(self):
        assert santim_to_etb(12550) == 125.5
        assert santim_to_etb(0) == 0.0

    @pytest.mark.parametrize(
        "amount,expected",
        [(125.5, 12550), (126400, 12640000), (19.99, 1999), (0.01, 1), (1.1, 110), (4.35, 435), (0.29, 29)],
    )
    def test_etb_to_santim_exact(self, amount, expected):
        # Includes values where float arithmetic (amount * 100) is off by one ulp.
        assert etb_to_santim(amount) == expected
        assert isinstance(etb_to_santim(amount), int)

    @pytest.mark.parametrize("amount", [0, -5, 0.285, 10.001, float("inf"), float("nan"), "abc"])
    def test_etb_to_santim_rejects(self, amount):
        with pytest.raises(ValueError):
            etb_to_santim(amount)


class TestEvidenceLadder:
    @pytest.mark.parametrize("level", ["L1", "L2", "L3"])
    def test_confirmable_levels_are_verified(self, level):
        assert verification_for(level) == "verified"

    @pytest.mark.parametrize("level", ["L4", "L5", "", None, "l2", "garbage"])
    def test_everything_else_is_unverified(self, level):
        assert verification_for(level) == "unverified"

    def test_l4_always_flags_receipt_photo(self):
        assert receipt_photo_on_file("L4", None, None) is True
        assert receipt_photo_on_file("L4", "cash note", "manual") is True

    def test_receipt_hint_flags_photo_but_never_changes_verification(self):
        assert receipt_photo_on_file("L2", "Receipt 123", None) is True
        assert receipt_photo_on_file("L2", "TT123", "telebirr_xlsx") is False
        assert receipt_photo_on_file(None, "receipt", None) is True
        assert verification_for("L4") == "unverified"

    def test_source_labels(self):
        assert describe_source("L2", None, "telebirr_xlsx") == "Telebirr statement line"
        assert describe_source("L2", None, "cbe_csv") == "Bank statement line"
        assert describe_source("L3", None, None) == "Buyer confirmation"
        assert describe_source("L4", None, "telegram_photo_receipt") == "Receipt photo only"
        assert describe_source("L4", "cash", None) == "Cash note only"
        assert describe_source("bogus", None, None) == "Unverified entry"


class TestDates:
    def test_relative_day(self):
        today = date(2026, 11, 20)
        assert relative_day(today, today) == "today"
        assert relative_day(date(2026, 11, 19), today) == "yesterday"
        assert relative_day(date(2026, 11, 15), today) == "5 days ago"
        assert relative_day(None, today) == NO_SALES_LABEL

    def test_format_timestamp(self):
        now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
        assert format_timestamp(datetime(2026, 11, 20, 9, 18, tzinfo=timezone.utc), now) == "Today, 09:18"
        assert format_timestamp(datetime(2026, 11, 19, 17, 42, tzinfo=timezone.utc), now) == "Yesterday, 17:42"
        assert format_timestamp(datetime(2026, 11, 14, 8, 5, tzinfo=timezone.utc), now) == "14 Nov, 08:05"
