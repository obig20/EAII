"""
tests/kernel/test_money.py

Tests for Rule 1: Money is integer santim.  Float is NEVER accepted.

Covers:
  - validate_santim rejects float
  - validate_santim rejects negative int
  - validate_santim rejects zero (when allow_zero=False)
  - validate_santim accepts positive int
  - to_etb produces correct string without floats
  - santim_from_etb_string parses correctly
  - santim_from_etb_string rejects >2 decimal places
"""

import pytest

from app.kernel.money import santim_from_etb_string, to_etb, validate_santim


# ---------------------------------------------------------------------------
# validate_santim
# ---------------------------------------------------------------------------
class TestValidateSantim:
    def test_rejects_float(self):
        """Rule 1: float input must raise TypeError immediately."""
        with pytest.raises(TypeError, match="float"):
            validate_santim(125.50)

    def test_rejects_float_whole(self):
        """Even a float that looks like an int (100.0) is rejected."""
        with pytest.raises(TypeError, match="float"):
            validate_santim(100.0)

    def test_rejects_negative(self):
        with pytest.raises(ValueError, match="negative"):
            validate_santim(-1)

    def test_rejects_zero_by_default(self):
        with pytest.raises(ValueError, match="greater than zero"):
            validate_santim(0)

    def test_allows_zero_when_flag_set(self):
        assert validate_santim(0, allow_zero=True) == 0

    def test_accepts_positive_int(self):
        assert validate_santim(12550) == 12550

    def test_accepts_one_santim(self):
        assert validate_santim(1) == 1

    def test_rejects_string(self):
        with pytest.raises(TypeError):
            validate_santim("125")  # type: ignore[arg-type]

    def test_rejects_none(self):
        with pytest.raises(TypeError):
            validate_santim(None)  # type: ignore[arg-type]

    def test_rejects_decimal(self):
        """Decimal type is not int and must be rejected."""
        from decimal import Decimal
        with pytest.raises(TypeError):
            validate_santim(Decimal("125.50"))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# to_etb
# ---------------------------------------------------------------------------
class TestToEtb:
    def test_standard_amount(self):
        assert to_etb(12550) == "125.50 ETB"

    def test_one_santim(self):
        assert to_etb(1) == "0.01 ETB"

    def test_one_birr(self):
        assert to_etb(100) == "1.00 ETB"

    def test_zero(self):
        assert to_etb(0) == "0.00 ETB"

    def test_large_amount(self):
        # 1,000,000 ETB = 100_000_000 santim
        assert to_etb(100_000_000) == "1000000.00 ETB"

    def test_nine_cents(self):
        assert to_etb(9) == "0.09 ETB"

    def test_rejects_float_input(self):
        with pytest.raises(TypeError):
            to_etb(125.50)  # type: ignore[arg-type]

    def test_negative_display(self):
        """Negative santim should display with minus sign (used in adjustments)."""
        assert to_etb(-100) == "-1.00 ETB"


# ---------------------------------------------------------------------------
# santim_from_etb_string
# ---------------------------------------------------------------------------
class TestSantimFromEtbString:
    def test_parse_standard(self):
        assert santim_from_etb_string("125.50") == 12550

    def test_parse_with_etb_suffix(self):
        assert santim_from_etb_string("125.50 ETB") == 12550

    def test_parse_whole_number(self):
        assert santim_from_etb_string("100") == 10000

    def test_parse_one_decimal(self):
        assert santim_from_etb_string("10.5") == 1050

    def test_rejects_more_than_two_decimals(self):
        with pytest.raises(ValueError, match="decimal places"):
            santim_from_etb_string("10.123")

    def test_rejects_non_string(self):
        with pytest.raises(TypeError):
            santim_from_etb_string(125)  # type: ignore[arg-type]

    def test_parse_zero(self):
        assert santim_from_etb_string("0.00") == 0

    def test_strips_whitespace(self):
        assert santim_from_etb_string("  50.00  ") == 5000
