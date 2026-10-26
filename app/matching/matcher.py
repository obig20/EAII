"""
app/matching/matcher.py

Deterministic, rule-based matching engine for credit sales and payments.
Adheres strictly to:
  - Rule 2: Evidence ladder (L1, L2, L3 can auto-confirm; L4 can ONLY be proposed).
  - Rule 3: Never auto-confirm if two or more invoices are equally plausible.
  - Rule 6: No LLM or AI calls — 100% deterministic, explainable rules.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Sequence

from app.kernel.enums import (
    CONFIRMABLE_EVIDENCE_LEVELS,
    EvidenceLevel,
    InvoiceState,
    MatchState,
    MatchTier,
    PaymentState,
)
from app.kernel.guards import assert_confirmable, assert_unambiguous
from app.kernel.models import Invoice, PaymentLine, PaymentMatch, Retailer
from app.matching.normalization import (
    match_name_in_text,
    normalize_account_number,
    normalize_ethiopian_name,
    normalize_phone,
    string_similarity,
)


@dataclass
class MatcherConfig:
    """Configurable parameters for matching tiers."""

    date_window_days: int = 60
    fuzzy_threshold: float = 0.88
    fuzzy_auto_confirm_threshold: float = 0.95
    fuzzy_margin: float = 0.15


@dataclass
class MatchItem:
    invoice_id: uuid.UUID
    matched_santim: int
    is_partial: bool = False
    is_combined: bool = False


@dataclass
class MatchResult:
    payment_line_id: uuid.UUID
    status: str  # "auto_confirmed", "proposed", "unmatched"
    tier: MatchTier | None = None
    matches: list[MatchItem] = field(default_factory=list)
    is_ambiguous: bool = False
    confidence_score: float = 0.0
    reason: str = ""
    evidence_level: EvidenceLevel = EvidenceLevel.L2


def _extract_invoice_tokens(text: str | None) -> list[str]:
    """
    Extract potential invoice identifiers from reference/narration strings.
    Matches forms like 'INV-001', 'INV102', 'INV 381', '381', etc.
    """
    if not text:
        return []
    clean = text.upper()
    tokens: list[str] = []

    # Direct invoice pattern matches: INV-123, INV123, INV#123
    for m in re.finditer(r"\b(?:INV|INVOICE)[-_\s#]*([A-Z0-9]+)\b", clean):
        token = m.group(1).strip()
        tokens.append(f"INV-{token}")
        tokens.append(f"INV{token}")
        tokens.append(token)

    # General word tokens that look like invoice numbers
    for m in re.finditer(r"\b([A-Z0-9]{3,12})\b", clean):
        token = m.group(1).strip()
        if any(c.isdigit() for c in token):
            tokens.append(token)

    return list(dict.fromkeys(tokens))  # preserve order, deduplicate


def _find_subset_sum(
    invoices: list[Invoice], target_santim: int, max_items: int = 3
) -> list[Invoice] | None:
    """
    Deterministic subset sum to find if 2 or 3 invoices exactly equal target payment.
    Used for proposing combined payments. Prunes invoices >= target_santim for performance.
    """
    valid_invoices = [inv for inv in invoices if 0 < inv.amount_santim < target_santim]
    if len(valid_invoices) < 2:
        return None

    from itertools import combinations

    for r in range(2, min(len(valid_invoices) + 1, max_items + 1)):
        for combo in combinations(valid_invoices, r):
            if sum(inv.amount_santim for inv in combo) == target_santim:
                return list(combo)
    return None


class PaymentMatcher:
    """Deterministic 3-tier payment matcher."""

    def __init__(self, config: MatcherConfig | None = None):
        self.config = config or MatcherConfig()

    def match_payment(
        self,
        payment: PaymentLine,
        open_invoices: Sequence[Invoice],
        retailers: Sequence[Retailer],
    ) -> MatchResult:
        """
        Evaluate a single PaymentLine against active open invoices and retailers.
        Deterministic tiers:
          Tier (a): Invoice number in reference AND equal amounts.
          Tier (b): Counterparty phone/account + amount + date window.
          Tier (c): Fuzzy retailer name + amount.
        """
        evidence = EvidenceLevel(payment.evidence_level)
        retailers_by_id = {r.id: r for r in retailers}

        # Filter out settled or void invoices
        active_invoices = [
            inv for inv in open_invoices if inv.state in ("open", "partially_paid")
        ]

        ref_text = f"{payment.raw_reference or ''} {payment.source_ref or ''}".strip()

        # Check if reference specifically matches an already-settled invoice (duplicate/overpayment)
        settled_invoices = [
            inv for inv in open_invoices if inv.state in ("paid", "settled")
        ]
        if ref_text and settled_invoices:
            matched_settled = []
            for inv in settled_invoices:
                inv_clean = inv.invoice_number.upper().strip()
                escaped_num = re.escape(inv_clean)
                pattern = rf"\b{escaped_num}\b"
                if re.search(pattern, ref_text, re.IGNORECASE):
                    matched_settled.append(inv)
            if matched_settled:
                target_inv = matched_settled[0]
                return MatchResult(
                    payment_line_id=payment.id,
                    status="proposed",
                    tier=MatchTier.TIER_A,
                    matches=[
                        MatchItem(
                            invoice_id=target_inv.id,
                            matched_santim=payment.amount_santim,
                        )
                    ],
                    confidence_score=0.75,
                    reason=f"Possible duplicate payment or overpayment for settled invoice {target_inv.invoice_number}",
                    evidence_level=evidence,
                )

        if not active_invoices:
            return MatchResult(
                payment_line_id=payment.id,
                status="unmatched",
                reason="No open invoices available",
                evidence_level=evidence,
            )

        # ------------------------------------------------------------------
        # TIER A: Invoice number in payment reference & amount checks
        # ------------------------------------------------------------------
        if ref_text:
            # Extract all distinct invoice-like tokens in ref_text
            raw_invoice_patterns = list(re.finditer(r"\b(?:INV|INVOICE)[-_\s#]*([A-Z0-9]+)\b", ref_text, re.IGNORECASE))
            distinct_tokens_in_ref = {m.group(1).upper().strip() for m in raw_invoice_patterns}
            is_compound_ref = len(distinct_tokens_in_ref) > 1

            matched_invoices: list[Invoice] = []
            for inv in active_invoices:
                inv_num_clean = inv.invoice_number.upper().strip()
                escaped_num = re.escape(inv_num_clean)
                pattern = rf"\b{escaped_num}\b"

                inv_digits = re.sub(r"^INV[-_\s#]*", "", inv_num_clean)
                alt_pattern = rf"\b(?:INV|INVOICE)[-_\s#]*{re.escape(inv_digits)}\b"

                if re.search(pattern, ref_text, re.IGNORECASE) or re.search(alt_pattern, ref_text, re.IGNORECASE):
                    matched_invoices.append(inv)

            if matched_invoices:
                exact_amount_matches = [
                    inv for inv in matched_invoices if inv.amount_santim == payment.amount_santim
                ]

                # SEC-11: Auto-confirm ONLY for Tier A exact token match with word boundaries,
                # equal amount, and exactly ONE invoice token in reference.
                if len(exact_amount_matches) == 1 and not is_compound_ref and len(matched_invoices) == 1:
                    target_inv = exact_amount_matches[0]
                    can_auto = evidence in CONFIRMABLE_EVIDENCE_LEVELS
                    return MatchResult(
                        payment_line_id=payment.id,
                        status="auto_confirmed" if can_auto else "proposed",
                        tier=MatchTier.TIER_A,
                        matches=[
                            MatchItem(
                                invoice_id=target_inv.id,
                                matched_santim=payment.amount_santim,
                            )
                        ],
                        confidence_score=1.0,
                        reason=f"Tier A exact match: {target_inv.invoice_number} in reference with equal amount",
                        evidence_level=evidence,
                    )
                elif len(exact_amount_matches) == 1:
                    # Compound reference or multiple candidate matches -> Review Queue proposal
                    target_inv = exact_amount_matches[0]
                    return MatchResult(
                        payment_line_id=payment.id,
                        status="proposed",
                        tier=MatchTier.TIER_A,
                        matches=[
                            MatchItem(
                                invoice_id=target_inv.id,
                                matched_santim=payment.amount_santim,
                            )
                        ],
                        confidence_score=0.85,
                        reason=f"Tier A compound reference proposal for {target_inv.invoice_number}",
                        evidence_level=evidence,
                    )
                elif len(exact_amount_matches) > 1:
                    # Ambiguous: send to review queue
                    return MatchResult(
                        payment_line_id=payment.id,
                        status="proposed",
                        tier=MatchTier.TIER_A,
                        matches=[
                            MatchItem(invoice_id=inv.id, matched_santim=payment.amount_santim)
                            for inv in exact_amount_matches
                        ],
                        is_ambiguous=True,
                        confidence_score=0.70,
                        reason=f"Ambiguous: {len(exact_amount_matches)} invoices matched reference with identical amounts",
                        evidence_level=evidence,
                    )
                elif len(matched_invoices) == 1:
                    target_inv = matched_invoices[0]
                    if payment.amount_santim < target_inv.amount_santim:
                        return MatchResult(
                            payment_line_id=payment.id,
                            status="proposed",
                            tier=MatchTier.TIER_A,
                            matches=[
                                MatchItem(
                                    invoice_id=target_inv.id,
                                    matched_santim=payment.amount_santim,
                                    is_partial=True,
                                )
                            ],
                            confidence_score=0.90,
                            reason=f"Proposed partial payment for {target_inv.invoice_number}",
                            evidence_level=evidence,
                        )
                elif len(matched_invoices) > 1:
                    combo = _find_subset_sum(matched_invoices, payment.amount_santim, max_items=len(matched_invoices))
                    target_invoices = combo if combo else matched_invoices[:3]
                    return MatchResult(
                        payment_line_id=payment.id,
                        status="proposed",
                        tier=MatchTier.TIER_A,
                        matches=[
                            MatchItem(
                                invoice_id=inv.id,
                                matched_santim=min(payment.amount_santim, inv.amount_santim),
                                is_combined=len(target_invoices) > 1,
                            )
                            for inv in target_invoices
                        ],
                        is_ambiguous=(not bool(combo)),
                        confidence_score=0.92 if combo else 0.80,
                        reason=f"Tier A compound reference for invoices: {', '.join(inv.invoice_number for inv in target_invoices)}",
                        evidence_level=evidence,
                    )
            else:
                # Delimiter and OCR typo matching (0/O substitutions, missing hyphens)
                clean_ref = re.sub(r"[-\s_#]+", "", ref_text.upper())
                clean_ref_0 = clean_ref.replace("O", "0")
                ocr_matches = []
                for inv in active_invoices:
                    norm_inv = re.sub(r"[-\s_#]+", "", inv.invoice_number.upper().strip())
                    norm_inv_0 = norm_inv.replace("O", "0")
                    if (norm_inv in clean_ref or norm_inv_0 in clean_ref_0) and len(norm_inv) >= 4:
                        ocr_matches.append(inv)
                if ocr_matches:
                    combo = _find_subset_sum(ocr_matches, payment.amount_santim, max_items=len(ocr_matches))
                    target_invoices = combo if combo else ocr_matches[:3]
                    return MatchResult(
                        payment_line_id=payment.id,
                        status="proposed",
                        tier=MatchTier.TIER_A,
                        matches=[
                            MatchItem(
                                invoice_id=inv.id,
                                matched_santim=min(payment.amount_santim, inv.amount_santim),
                                is_combined=len(target_invoices) > 1,
                                is_partial=payment.amount_santim < inv.amount_santim,
                            )
                            for inv in target_invoices
                        ],
                        is_ambiguous=(len(ocr_matches) > 1 and not bool(combo)),
                        confidence_score=0.88,
                        reason=f"Tier A OCR reference match for {len(target_invoices)} invoice(s)",
                        evidence_level=evidence,
                    )

        # ------------------------------------------------------------------
        # TIER B: Counterparty phone or account + amount + date window
        # (SEC-11: Tier B is NEVER auto-confirmed; always routed to review queue)
        # ------------------------------------------------------------------
        norm_cp_phone = normalize_phone(payment.counterparty_account) or normalize_phone(
            payment.counterparty_name
        )
        norm_cp_acc = normalize_account_number(payment.counterparty_account)

        matched_retailers: list[Retailer] = []
        for ret in retailers:
            phone_match = norm_cp_phone and (normalize_phone(ret.phone) == norm_cp_phone)
            acc_match = norm_cp_acc and (
                normalize_account_number(ret.bank_account) == norm_cp_acc
            )
            if phone_match or acc_match:
                matched_retailers.append(ret)

        if len(matched_retailers) == 1:
            ret = matched_retailers[0]
            ret_invoices = [inv for inv in active_invoices if inv.retailer_id == ret.id]

            date_window = self.config.date_window_days
            equal_amount_invoices = [
                inv
                for inv in ret_invoices
                if inv.amount_santim == payment.amount_santim
                and abs((payment.value_date - inv.due_date).days) <= date_window
            ]

            if len(equal_amount_invoices) == 1:
                target_inv = equal_amount_invoices[0]
                return MatchResult(
                    payment_line_id=payment.id,
                    status="proposed",
                    tier=MatchTier.TIER_B,
                    matches=[
                        MatchItem(
                            invoice_id=target_inv.id,
                            matched_santim=payment.amount_santim,
                        )
                    ],
                    confidence_score=0.90,
                    reason=f"Tier B counterparty match for {ret.name}: invoice {target_inv.invoice_number} proposed for review",
                    evidence_level=evidence,
                )
            elif len(equal_amount_invoices) > 1:
                sorted_by_date = sorted(
                    equal_amount_invoices,
                    key=lambda inv: min(
                        abs((payment.value_date - inv.issued_date).days),
                        abs((payment.value_date - inv.due_date).days),
                    ),
                )
                target_inv = sorted_by_date[0]
                return MatchResult(
                    payment_line_id=payment.id,
                    status="proposed",
                    tier=MatchTier.TIER_B,
                    matches=[
                        MatchItem(
                            invoice_id=inv.id,
                            matched_santim=payment.amount_santim,
                        )
                        for inv in sorted_by_date[:3]
                    ],
                    is_ambiguous=True,
                    confidence_score=0.85,
                    reason=f"Tier B ambiguous amount collision for {ret.name}: invoice {target_inv.invoice_number}",
                    evidence_level=evidence,
                )

            # Check combined payment (single payment covering 2-3 invoices)
            combined_combo = _find_subset_sum(ret_invoices, payment.amount_santim, max_items=3)
            if combined_combo:
                # One payment covering several invoices is only PROPOSED for owner approval
                return MatchResult(
                    payment_line_id=payment.id,
                    status="proposed",
                    tier=MatchTier.TIER_B,
                    matches=[
                        MatchItem(
                            invoice_id=inv.id,
                            matched_santim=inv.amount_santim,
                            is_combined=True,
                        )
                        for inv in combined_combo
                    ],
                    confidence_score=0.92,
                    reason=f"Proposed combined payment covering {len(combined_combo)} invoices for {ret.name}",
                    evidence_level=evidence,
                )

            # Check partial payment (single open invoice with larger amount)
            if len(ret_invoices) == 1 and payment.amount_santim < ret_invoices[0].amount_santim:
                target_inv = ret_invoices[0]
                return MatchResult(
                    payment_line_id=payment.id,
                    status="proposed",
                    tier=MatchTier.TIER_B,
                    matches=[
                        MatchItem(
                            invoice_id=target_inv.id,
                            matched_santim=payment.amount_santim,
                            is_partial=True,
                        )
                    ],
                    confidence_score=0.85,
                    reason=f"Proposed partial payment for {ret.name} invoice {target_inv.invoice_number}",
                    evidence_level=evidence,
                )

        # ------------------------------------------------------------------
        # TIER C: Fuzzy retailer name + amount
        # ------------------------------------------------------------------
        name_candidates = [payment.counterparty_name, payment.raw_reference]
        name_text = " ".join(filter(None, name_candidates)).strip()

        if name_text:
            scored_retailers: list[tuple[float, Retailer]] = []
            for ret in retailers:
                score = match_name_in_text(ret.name, name_text)
                if score >= self.config.fuzzy_threshold:
                    scored_retailers.append((score, ret))

            scored_retailers.sort(key=lambda x: x[0], reverse=True)

            if scored_retailers:
                top_score, top_ret = scored_retailers[0]
                # Check margin to second match to prevent collision
                margin_ok = (
                    len(scored_retailers) == 1
                    or (top_score - scored_retailers[1][0]) >= self.config.fuzzy_margin
                )

                if margin_ok:
                    # Enforce date window in Tier C just as in Tier B
                    ret_invoices = [
                        inv
                        for inv in active_invoices
                        if inv.retailer_id == top_ret.id
                        and inv.amount_santim == payment.amount_santim
                        and abs((payment.value_date - inv.due_date).days) <= self.config.date_window_days
                    ]

                    if len(ret_invoices) == 1:
                        target_inv = ret_invoices[0]
                        # SEC-11: Tier C fuzzy matches are NEVER auto-confirmed; always proposed for review queue
                        return MatchResult(
                            payment_line_id=payment.id,
                            status="proposed",
                            tier=MatchTier.TIER_C,
                            matches=[
                                MatchItem(
                                    invoice_id=target_inv.id,
                                    matched_santim=payment.amount_santim,
                                )
                            ],
                            confidence_score=top_score,
                            reason=f"Tier C fuzzy name match ({top_score:.2f}) for {top_ret.name}: invoice {target_inv.invoice_number}",
                            evidence_level=evidence,
                        )
                    elif len(ret_invoices) > 1:
                        return MatchResult(
                            payment_line_id=payment.id,
                            status="proposed",
                            tier=MatchTier.TIER_C,
                            matches=[
                                MatchItem(invoice_id=inv.id, matched_santim=payment.amount_santim)
                                for inv in ret_invoices
                            ],
                            is_ambiguous=True,
                            confidence_score=top_score * 0.7,
                            reason=f"Ambiguous: Fuzzy match {top_ret.name} has multiple invoices with equal amount",
                            evidence_level=evidence,
                        )
                    else:
                        # Outside date window (delayed payment) -> PROPOSED only!
                        delayed_invoices = [
                            inv
                            for inv in active_invoices
                            if inv.retailer_id == top_ret.id
                            and inv.amount_santim == payment.amount_santim
                        ]
                        if delayed_invoices:
                            return MatchResult(
                                payment_line_id=payment.id,
                                status="proposed",  # Delayed payments outside window are NEVER auto-confirmed!
                                tier=MatchTier.TIER_C,
                                matches=[
                                    MatchItem(
                                        invoice_id=inv.id,
                                        matched_santim=payment.amount_santim,
                                    )
                                    for inv in delayed_invoices
                                ],
                                confidence_score=0.75,
                                reason=f"Proposed late payment outside date window for {top_ret.name}",
                                evidence_level=evidence,
                            )

        # ------------------------------------------------------------------
        # AMOUNT FALLBACK: Open invoices matching amount within date window
        # (RULE 3: Multiple plausible invoices -> Review Queue proposed ONLY)
        # ------------------------------------------------------------------
        amt_invoices = [
            inv for inv in active_invoices
            if inv.amount_santim == payment.amount_santim
            and abs((payment.value_date - inv.due_date).days) <= self.config.date_window_days
        ]
        if amt_invoices:
            sorted_amt = sorted(
                amt_invoices,
                key=lambda i: min(
                    abs((payment.value_date - i.issued_date).days),
                    abs((payment.value_date - i.due_date).days),
                ),
            )
            return MatchResult(
                payment_line_id=payment.id,
                status="proposed",
                tier=MatchTier.TIER_C,
                matches=[
                    MatchItem(invoice_id=i.id, matched_santim=payment.amount_santim)
                    for i in sorted_amt[:3]
                ],
                is_ambiguous=(len(amt_invoices) > 1),
                confidence_score=0.60,
                reason=f"Proposed amount match for {len(amt_invoices)} invoice(s)",
                evidence_level=evidence,
            )

        # ------------------------------------------------------------------
        # UNMATCHED
        # ------------------------------------------------------------------
        return MatchResult(
            payment_line_id=payment.id,
            status="unmatched",
            reason="No matching invoice found in any tier",
            evidence_level=evidence,
        )

    def apply_match_results(
        self,
        session,
        payment: PaymentLine,
        result: MatchResult,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> list[PaymentMatch]:
        """
        Persist PaymentMatch records and update PaymentLine/Invoice states,
        recording append-only events (Rule 4) and enforcing evidence guards (Rule 2).
        """
        from datetime import datetime, timezone
        from app.kernel.events import append_event

        matches_created: list[PaymentMatch] = []
        now = datetime.now(timezone.utc)

        if result.status == "auto_confirmed":
            # Rule 2: Evidence guard verification
            assert_confirmable(result.evidence_level)
            # Rule 3: Unambiguous verification
            assert_unambiguous([m.invoice_id for m in result.matches])

            payment.state = PaymentState.CONFIRMED.value
            for m in result.matches:
                pm = PaymentMatch(
                    id=uuid.uuid4(),
                    tenant_id=payment.tenant_id,
                    payment_line_id=payment.id,
                    invoice_id=m.invoice_id,
                    matched_santim=m.matched_santim,
                    tier=(result.tier.value if result.tier else MatchTier.TIER_A.value),
                    state=MatchState.CONFIRMED.value,
                    confirmed_by=actor_id,
                    confirmed_at=now,
                    created_at=now,
                )
                session.add(pm)
                matches_created.append(pm)

                # Update invoice state upon auto-confirmation
                from sqlalchemy import func, select
                inv = session.get(Invoice, m.invoice_id)
                if inv:
                    inv_paid = session.execute(
                        select(func.coalesce(func.sum(PaymentMatch.matched_santim), 0))
                        .where(
                            PaymentMatch.invoice_id == inv.id,
                            PaymentMatch.state == MatchState.CONFIRMED.value,
                            PaymentMatch.id != pm.id,
                        )
                    ).scalar_one() + m.matched_santim
                    if inv_paid >= inv.amount_santim:
                        inv.state = InvoiceState.SETTLED.value
                    else:
                        inv.state = InvoiceState.PARTIALLY_PAID.value

                # Append event
                append_event(
                    session,
                    tenant_id=payment.tenant_id,
                    event_type="payment.auto_confirmed",
                    aggregate_type="payment_line",
                    aggregate_id=payment.id,
                    actor_id=actor_id,
                    evidence_level=result.evidence_level,
                    payload={
                        "invoice_id": str(m.invoice_id),
                        "matched_santim": m.matched_santim,
                        "tier": pm.tier,
                        "reason": result.reason,
                    },
                )
        elif result.status == "proposed":
            payment.state = PaymentState.PROPOSED.value
            for m in result.matches:
                pm = PaymentMatch(
                    id=uuid.uuid4(),
                    tenant_id=payment.tenant_id,
                    payment_line_id=payment.id,
                    invoice_id=m.invoice_id,
                    matched_santim=m.matched_santim,
                    tier=(result.tier.value if result.tier else MatchTier.TIER_A.value),
                    state=MatchState.PROPOSED.value,
                    created_at=now,
                )
                session.add(pm)
                matches_created.append(pm)

                append_event(
                    session,
                    tenant_id=payment.tenant_id,
                    event_type="payment.proposed",
                    aggregate_type="payment_line",
                    aggregate_id=payment.id,
                    actor_id=actor_id,
                    evidence_level=result.evidence_level,
                    payload={
                        "invoice_id": str(m.invoice_id),
                        "matched_santim": m.matched_santim,
                        "tier": pm.tier,
                        "is_ambiguous": result.is_ambiguous,
                        "reason": result.reason,
                    },
                )

        session.flush()
        return matches_created
