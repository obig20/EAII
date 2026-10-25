"""
app/kernel/money.py

Money is always stored and calculated as integer santim.
1 ETB = 100 santim.

Rules (non-negotiable):
  - Amount values are plain Python int.  float is NEVER accepted.
  - Zero and negative values are invalid for payment/invoice amounts.
  - Display helpers convert to ETB string without floats.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Type alias — use this in type hints throughout the codebase
# ---------------------------------------------------------------------------
Santim = int


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------
def validate_santim(value: object, *, allow_zero: bool = False) -> Santim:
    """
    Validate and return a santim amount.

    Args:
        value: The value to validate.
        allow_zero: If False (default), zero is also rejected.

    Raises:
        TypeError:  value is a float or not numeric.
        ValueError: value is negative (or zero when allow_zero=False).

    Returns:
        The validated int amount in santim.
    """
    if isinstance(value, float):
        raise TypeError(
            f"Money amounts must be integer santim, not float. "
            f"Got: {value!r}. "
            f"Multiply ETB by 100 and pass an int (e.g. 125.50 ETB → 12550 santim)."
        )
    if not isinstance(value, int):
        raise TypeError(
            f"Money amounts must be int (santim), got {type(value).__name__!r}: {value!r}"
        )
    if value < 0:
        raise ValueError(f"Santim amount must not be negative, got {value}.")
    if not allow_zero and value == 0:
        raise ValueError(
            "Santim amount must be greater than zero for invoice and payment amounts."
        )
    return value


# ---------------------------------------------------------------------------
# Display helpers (no floats)
# ---------------------------------------------------------------------------
def to_etb(santim: Santim) -> str:
    """
    Convert a santim integer to a human-readable ETB string.

    Uses integer arithmetic only — no floats.

    Examples:
        to_etb(12550)  -> "125.50 ETB"
        to_etb(100)    -> "1.00 ETB"
        to_etb(1)      -> "0.01 ETB"
        to_etb(0)      -> "0.00 ETB"
    """
    if not isinstance(santim, int):
        raise TypeError(f"to_etb expects int santim, got {type(santim).__name__!r}")
    sign = "-" if santim < 0 else ""
    abs_santim = abs(santim)
    birr = abs_santim // 100
    cents = abs_santim % 100
    return f"{sign}{birr}.{cents:02d} ETB"


def santim_from_etb_string(etb_str: str) -> Santim:
    """
    Parse a decimal ETB string (e.g. "125.50") to integer santim.

    Raises:
        ValueError: If the string is not a valid decimal.
        TypeError:  If the argument is not a string.
    """
    if not isinstance(etb_str, str):
        raise TypeError(f"Expected str, got {type(etb_str).__name__!r}")
    etb_str = etb_str.strip().upper().removesuffix("ETB").strip()
    if "." in etb_str:
        birr_part, cents_part = etb_str.split(".", 1)
        if len(cents_part) > 2:
            raise ValueError(
                f"ETB value has more than 2 decimal places: {etb_str!r}. "
                "Truncation is not allowed — check the source data."
            )
        cents_part = cents_part.ljust(2, "0")
    else:
        birr_part = etb_str
        cents_part = "00"

    try:
        birr = int(birr_part)
        cents = int(cents_part)
    except ValueError as exc:
        raise ValueError(f"Cannot parse ETB string: {etb_str!r}") from exc

    return birr * 100 + cents
