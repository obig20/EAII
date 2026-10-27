"""
app/kernel/tokens.py

Cryptographic token utilities for Daily Close.
Implements scoped HMAC-SHA256 signed stale-view tokens for match approval idempotency
and concurrency protection, with 30-minute lifetime, user/tenant binding, and safe parsing.
Also provides compact 8-character HMAC tokens for Telegram callback data (under 64 bytes).
"""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from typing import Any

TOKEN_MAX_AGE_SECONDS = 1800  # 30 minutes lifetime


def hash_approval_token(token: str) -> str:
    """Return SHA-256 hex digest of the approval token for idempotent lookup."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_match_approval_token(
    secret: str,
    *,
    tenant_id: uuid.UUID | str,
    user_id: uuid.UUID | str,
    match_id: uuid.UUID | str,
    matched_santim: int,
    invoice_amount_santim: int,
    payment_amount_santim: int,
    match_state: str,
    issued_at: int | None = None,
) -> str:
    """
    Generate a scoped HMAC-SHA256 signed stale-view token.
    Encompasses tenant id, user id, match id, matched amount, target invoice total,
    payment line total, match state, and issue timestamp.
    """
    ts = issued_at if issued_at is not None else int(time.time())
    payload = (
        f"{tenant_id}:{user_id}:{match_id}:{matched_santim}:"
        f"{invoice_amount_santim}:{payment_amount_santim}:{match_state}:{ts}"
    )
    sig = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def verify_match_approval_token(
    secret: str,
    token: str,
    *,
    expected_tenant_id: uuid.UUID | str,
    expected_user_id: uuid.UUID | str,
    expected_match_id: uuid.UUID | str,
    current_matched_santim: int,
    current_invoice_amount_santim: int,
    current_payment_amount_santim: int,
    current_match_state: str,
    current_time: int | None = None,
) -> tuple[bool, str]:
    """
    Verify HMAC signature, scope (tenant, user, match), expiry (30m), and snapshot parity.
    Rejects:
      - Malformed or tampered tokens
      - Expired tokens (> 30 minutes)
      - Wrong user or tenant context
      - Reused tokens on different matches
      - Stale views where amounts, match state, or totals diverged
      - Reversed matches (cannot be re-approved)
    """
    if not token or not isinstance(token, str):
        return False, "Approval token is missing or invalid."

    parts = token.strip().split(":")
    if len(parts) != 9:
        return False, "Malformed approval token structure."

    (
        token_tid,
        token_uid,
        token_mid,
        token_matched,
        token_inv,
        token_pl,
        token_state,
        token_ts,
        token_sig,
    ) = parts

    # 1. Constant-time signature verification
    reconstructed_payload = (
        f"{token_tid}:{token_uid}:{token_mid}:{token_matched}:"
        f"{token_inv}:{token_pl}:{token_state}:{token_ts}"
    )
    expected_sig = hmac.new(
        secret.encode("utf-8"), reconstructed_payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected_sig, token_sig):
        return False, "Token signature verification failed."

    # 2. Scope binding verification
    if token_tid != str(expected_tenant_id):
        return False, "Token belongs to a different tenant."
    if token_uid != str(expected_user_id):
        return False, "Token was issued for a different user."
    if token_mid != str(expected_match_id):
        return False, "Token was issued for a different match."

    # 3. Expiration check (30 minutes)
    try:
        ts_int = int(token_ts)
    except ValueError:
        return False, "Invalid token timestamp."

    now = current_time if current_time is not None else int(time.time())
    if now - ts_int > TOKEN_MAX_AGE_SECONDS or ts_int > now + 60:
        return False, "Approval token has expired (30-minute limit)."

    # 4. Guard against re-approval of reversed matches
    if current_match_state == "reversed" or token_state == "reversed":
        return False, "A reversed match can never be re-approved."

    # 5. Database snapshot parity check (stale view detection)
    try:
        if int(token_matched) != current_matched_santim:
            return False, f"Matched amount changed from {token_matched} to {current_matched_santim} santim."
        if int(token_inv) != current_invoice_amount_santim:
            return False, f"Invoice amount changed from {token_inv} to {current_invoice_amount_santim} santim."
        if int(token_pl) != current_payment_amount_santim:
            return False, f"Payment line amount changed from {token_pl} to {current_payment_amount_santim} santim."
    except ValueError:
        return False, "Token amounts are not valid integers."

    if token_state != current_match_state:
        return False, f"Match state changed from {token_state} to {current_match_state}."

    return True, "Valid"


def generate_telegram_match_token(secret: str, match_id: uuid.UUID | str) -> str:
    """Generate compact 8-character HMAC signature for Telegram callback data."""
    mid_str = str(match_id)
    return hmac.new(secret.encode("utf-8"), mid_str.encode("utf-8"), hashlib.sha256).hexdigest()[:8]


def verify_telegram_match_token(secret: str, match_id: uuid.UUID | str, token: str) -> bool:
    """Verify compact 8-character HMAC signature."""
    expected = generate_telegram_match_token(secret, match_id)
    return hmac.compare_digest(expected, token)
