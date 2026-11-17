"""
app/matching/payer_memory.py

Payer Memory module for remembering third-party payers (spouse, sibling, employee)
associated with retailers in Ethiopian wholesale trade.

Invariants:
  1. Owner-only creation and deletion; never created automatically.
  2. Proposals generated from payer memory are STRICTLY 'proposed' (never 'auto_confirmed' or 'confirmed').
  3. matcher.py's auto-confirm conditions remain 100% untouched.
  4. Ambiguity guard: If a payer maps to more than one retailer, it is flagged as ambiguous and routed to owner review.
  5. Multi-tenant RLS isolation: All queries filter by tenant_id.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.kernel.enums import EvidenceLevel, InvoiceState, MatchTier
from app.kernel.events import append_event
from app.kernel.models import Invoice, PaymentLine, PayerAlias, Retailer
from app.matching.matcher import MatchItem, MatchResult
from app.matching.normalization import (
    normalize_account_number,
    normalize_ethiopian_name,
    normalize_phone,
    string_similarity,
)


def normalize_payer_name(raw_name: str | None) -> str | None:
    """Normalize raw payer name using Ethiopian phonetic / transliteration rules."""
    if not raw_name:
        return None
    normalized = normalize_ethiopian_name(raw_name).strip()
    return normalized if normalized else None


def normalize_payer_reference(raw_ref: str | None) -> str | None:
    """Normalize phone number, account number, or generic reference token."""
    if not raw_ref:
        return None
    cleaned = raw_ref.strip()
    phone = normalize_phone(cleaned)
    if phone:
        return phone
    acct = normalize_account_number(cleaned)
    if acct:
        return acct
    # Strip all non-alphanumeric characters
    alphanum = re.sub(r"[^A-Za-z0-9]", "", cleaned).upper()
    return alphanum if alphanum else None


def create_payer_alias(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    retailer_id: uuid.UUID,
    raw_payer_name: str | None,
    raw_payer_ref: str | None,
    user_id: uuid.UUID,
) -> PayerAlias:
    """
    Link a third-party payer (name and/or reference) to a retailer.
    Owner-only operation; records an immutable event in the audit log.
    """
    clean_name = raw_payer_name.strip() if raw_payer_name else None
    clean_ref = raw_payer_ref.strip() if raw_payer_ref else None

    if not clean_name and not clean_ref:
        raise ValueError("At least one of payer name or reference/phone must be provided.")

    norm_name = normalize_payer_name(clean_name)
    norm_ref = normalize_payer_reference(clean_ref)

    # Check for existing active alias for this retailer
    existing = session.execute(
        select(PayerAlias).where(
            PayerAlias.tenant_id == tenant_id,
            PayerAlias.retailer_id == retailer_id,
            PayerAlias.normalized_payer_name == norm_name,
            PayerAlias.normalized_payer_ref == norm_ref,
            PayerAlias.deleted_at.is_(None),
        )
    ).scalar_one_or_none()

    if existing:
        return existing

    alias = PayerAlias(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        retailer_id=retailer_id,
        normalized_payer_name=norm_name,
        normalized_payer_ref=norm_ref,
        raw_payer_name=clean_name,
        raw_payer_ref=clean_ref,
        created_by=user_id,
        created_at=datetime.now(timezone.utc),
    )
    session.add(alias)
    session.flush()

    append_event(
        session,
        tenant_id=tenant_id,
        event_type="payer_alias.created",
        aggregate_type="payer_alias",
        aggregate_id=alias.id,
        actor_id=user_id,
        payload={
            "alias_id": str(alias.id),
            "retailer_id": str(retailer_id),
            "raw_payer_name": clean_name,
            "raw_payer_ref": clean_ref,
            "normalized_payer_name": norm_name,
            "normalized_payer_ref": norm_ref,
        },
    )
    return alias


def delete_payer_alias(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    alias_id: uuid.UUID,
    user_id: uuid.UUID,
) -> bool:
    """Soft-delete a payer alias and record an audit event."""
    alias = session.execute(
        select(PayerAlias).where(
            PayerAlias.tenant_id == tenant_id,
            PayerAlias.id == alias_id,
            PayerAlias.deleted_at.is_(None),
        )
    ).scalar_one_or_none()

    if not alias:
        return False

    alias.deleted_at = datetime.now(timezone.utc)
    session.flush()

    append_event(
        session,
        tenant_id=tenant_id,
        event_type="payer_alias.deleted",
        aggregate_type="payer_alias",
        aggregate_id=alias.id,
        actor_id=user_id,
        payload={"alias_id": str(alias.id)},
    )
    return True


def list_payer_aliases(
    session: Session,
    *,
    tenant_id: uuid.UUID,
) -> list[PayerAlias]:
    """Retrieve all active payer aliases for a tenant, joined with retailers."""
    return list(
        session.execute(
            select(PayerAlias)
            .where(
                PayerAlias.tenant_id == tenant_id,
                PayerAlias.deleted_at.is_(None),
            )
            .order_by(PayerAlias.created_at.desc())
        ).scalars().all()
    )


def find_matching_payer_aliases(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    payment_line: PaymentLine,
) -> list[PayerAlias]:
    """
    Search active aliases matching the payment line's counterparty name,
    account, or reference.
    """
    active_aliases = list_payer_aliases(session, tenant_id=tenant_id)
    if not active_aliases:
        return []

    line_name = normalize_payer_name(payment_line.counterparty_name)
    line_ref = normalize_payer_reference(payment_line.counterparty_account or payment_line.raw_reference)

    matched: list[PayerAlias] = []
    for alias in active_aliases:
        match_found = False

        # 1. Match on reference / account / phone
        if alias.normalized_payer_ref and line_ref:
            if alias.normalized_payer_ref == line_ref:
                match_found = True

        # 2. Match on counterparty name
        if not match_found and alias.normalized_payer_name and line_name:
            if alias.normalized_payer_name == line_name:
                match_found = True
            elif string_similarity(alias.normalized_payer_name, line_name) >= 0.85:
                match_found = True

        if match_found:
            matched.append(alias)

    return matched


def evaluate_payer_memory_proposal(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    payment_line: PaymentLine,
    open_invoices: Sequence[Invoice],
    retailers: Sequence[Retailer],
) -> MatchResult | None:
    """
    Build a proposed MatchResult from active payer aliases.
    
    GUARANTEES:
      - ALWAYS returns status='proposed' (NEVER 'auto_confirmed' or 'confirmed').
      - If mapped to more than 1 retailer, flags ambiguity (Rule 3).
      - Reason states: 'Known payer, you confirmed on YYYY-MM-DD'.
    """
    matched_aliases = find_matching_payer_aliases(session, tenant_id=tenant_id, payment_line=payment_line)
    if not matched_aliases:
        return None

    retailers_by_id = {r.id: r for r in retailers}
    distinct_retailer_ids = list({a.retailer_id for a in matched_aliases})

    # Case 1: Ambiguity — Payer maps to more than one retailer
    if len(distinct_retailer_ids) > 1:
        mapped_names = [
            retailers_by_id[rid].name for rid in distinct_retailer_ids if rid in retailers_by_id
        ]
        names_str = ", ".join(mapped_names) if mapped_names else "multiple retailers"
        return MatchResult(
            payment_line_id=payment_line.id,
            status="proposed",
            tier=MatchTier.TIER_B,
            matches=[],
            is_ambiguous=True,
            confidence_score=0.5,
            reason=f"Ambiguous: Payer mapped to multiple retailers ({names_str}). Requires owner selection.",
            evidence_level=EvidenceLevel(payment_line.evidence_level),
        )

    # Case 2: Payer maps to exactly ONE retailer
    target_retailer_id = distinct_retailer_ids[0]
    matched_alias = next(a for a in matched_aliases if a.retailer_id == target_retailer_id)
    confirmed_date = matched_alias.created_at.strftime("%Y-%m-%d")

    # Find open invoices for this retailer
    candidate_invoices = [
        inv for inv in open_invoices
        if inv.retailer_id == target_retailer_id
        and inv.state in (InvoiceState.OPEN.value, InvoiceState.PARTIALLY_PAID.value)
    ]

    matches: list[MatchItem] = []
    # Try to find exact amount match first
    exact_inv = next((inv for inv in candidate_invoices if inv.amount_santim == payment_line.amount_santim), None)
    if exact_inv:
        matches.append(MatchItem(invoice_id=exact_inv.id, matched_santim=payment_line.amount_santim))
    elif candidate_invoices:
        # Match oldest open invoice (partial or differing)
        sorted_invs = sorted(candidate_invoices, key=lambda x: x.issued_date)
        oldest_inv = sorted_invs[0]
        allocated = min(oldest_inv.amount_santim, payment_line.amount_santim)
        matches.append(MatchItem(invoice_id=oldest_inv.id, matched_santim=allocated, is_partial=(allocated < oldest_inv.amount_santim)))

    return MatchResult(
        payment_line_id=payment_line.id,
        status="proposed",  # STRICT: ALWAYS proposed, NEVER confirmed
        tier=MatchTier.TIER_B,
        matches=matches,
        is_ambiguous=False,
        confidence_score=0.85,
        reason=f"Known payer, you confirmed on {confirmed_date}",
        evidence_level=EvidenceLevel(payment_line.evidence_level),
    )
