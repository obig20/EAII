"""
app/matching/synthetic.py

Deterministic synthetic data generator with known ground truth for evaluating
matching accuracy and zero false confirmation invariants.

Generates:
  - 30 realistic Ethiopian retailers (names, Amharic transliterations, phones, bank accounts)
  - 300 invoices (due dates, positive santim amounts, unique invoice numbers)
  - 400 payment lines with injected noise categories:
      1. Missing references
      2. English & Amharic-transliterated name variations
      3. Partial payments
      4. Combined payments (covering 2-3 invoices)
      5. Duplicate lines
      6. Same-amount collisions
      7. Date delays
      8. L4 evidence lines
      9. Unmatched noise lines
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from app.kernel.enums import EvidenceLevel
from app.kernel.models import Invoice, PaymentLine, Retailer


# Realistic Ethiopian retail store names & transliteration pairs
RETAILER_SEEDS = [
    ("Abebe Kebede", "Abebe Kebede General Trading", "0911234001", "10001234001"),
    ("Almaz Ayana", "Almaz Shop", "0911234002", "10001234002"),
    ("Mohammed Ali", "Mohamed Trading PLC", "0911234003", "10001234003"),
    ("Kassahun Desta", "Kasahun Suq", "0911234004", "10001234004"),
    ("Chala Bekele", "Chala Bekele Wholesaler", "0911234005", "10001234005"),
    ("Fatuma Roba", "Fatuma Roba Consumer Goods", "0911234006", "10001234006"),
    ("Tilahun Gessesse", "Tilahun General Store", "0911234007", "10001234007"),
    ("Hiwot Tefera", "Hiwot Tefera Retailer", "0911234008", "10001234008"),
    ("Yonas Berhanu", "Yonas Berhanu & Sons", "0911234009", "10001234009"),
    ("Selamawit Gebre", "Selam Mini Market", "0911234010", "10001234010"),
    ("Mulugeta Tesfaye", "Mulugeta Suq Merkato", "0911234011", "10001234011"),
    ("Genet Assefa", "Genet Enterprises", "0911234012", "10001234012"),
    ("Getachew Reda", "Getachew Store Piassa", "0911234013", "10001234013"),
    ("Tewodros Kassahun", "Teddy Shop Bole", "0911234014", "10001234014"),
    ("Biruk Hailu", "Biruk & Brothers", "0911234015", "10001234015"),
    ("Meseret Defar", "Meseret Defar Grocery", "0911234016", "10001234016"),
    ("Dawit Tsige", "Dawit Tsige Trading", "0911234017", "10001234017"),
    ("Tigist Assefa", "Tigist Provisions", "0911234018", "10001234018"),
    ("Haile Gebrselassie", "Haile Supermarket", "0911234019", "10001234019"),
    ("Aster Aweke", "Aster Boutique & Retail", "0911234020", "10001234020"),
    ("Solomon Bogale", "Solomon Bogale Stationery", "0911234021", "10001234021"),
    ("Rahel Getu", "Rahel Cosmetics & Goods", "0911234022", "10001234022"),
    ("Fikadu Tadesse", "Fikadu Wholesale Depot", "0911234023", "10001234023"),
    ("Eleni Gabre", "Eleni Commodity Store", "0911234024", "10001234024"),
    ("Berhanu Nega", "Berhanu General Goods", "0911234025", "10001234025"),
    ("Samrawit Fikru", "Samrawit Distribution", "0911234026", "10001234026"),
    ("Henok Mulatu", "Henok Electronics & Goods", "0911234027", "10001234027"),
    ("Martha Wolde", "Martha Bakery & Retail", "0911234028", "10001234028"),
    ("Kebede Michael", "Kebede Michael Bookshop", "0911234029", "10001234029"),
    ("Zewditu Menelik", "Zewditu Spices & Foodstuffs", "0911234030", "10001234030"),
]

# Transliteration aliases for noise injection
TRANSLITERATION_MAP = {
    "Abebe": "Abebbe",
    "Mohammed": "Muhammed",
    "Kassahun": "Kasahun",
    "Tilahun": "Telahun",
    "Hiwot": "Hewan",
    "Yonas": "Yohanis",
    "Mulugeta": "Mulu",
    "Getachew": "Getcho",
    "Meseret": "Mese",
    "Dawit": "David",
    "Haile": "Hayle",
}


@dataclass
class SyntheticPaymentGroundTruth:
    payment_line: PaymentLine
    expected_status: str  # "auto_confirmed", "proposed", "unmatched"
    expected_invoice_ids: list[uuid.UUID]
    noise_type: str  # "clean_tier_a", "clean_tier_b", "missing_reference", "transliteration", "partial", "combined", "duplicate", "collision", "date_delay", "l4_evidence", "unmatched_noise"
    is_auto_confirmable: bool


@dataclass
class SyntheticDataSet:
    tenant_id: uuid.UUID
    retailers: list[Retailer]
    invoices: list[Invoice]
    payments_with_truth: list[SyntheticPaymentGroundTruth]

    @property
    def payment_lines(self) -> list[PaymentLine]:
        return [pt.payment_line for pt in self.payments_with_truth]


def generate_synthetic_dataset(
    *,
    seed: int = 42,
    tenant_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    base_date: date | None = None,
) -> SyntheticDataSet:
    """
    Generate deterministic test dataset:
      - 30 retailers
      - 300 invoices
      - 400 payment lines with ground truth
    """
    rng = random.Random(seed)
    tid = tenant_id or uuid.uuid4()
    uid = user_id or uuid.uuid4()
    start_date = base_date or date(2026, 8, 1)

    # -----------------------------------------------------------------------
    # 1. Retailers (30)
    # -----------------------------------------------------------------------
    retailers: list[Retailer] = []
    for owner_name, shop_name, phone, acc in RETAILER_SEEDS:
        ret = Retailer(
            id=uuid.uuid4(),
            tenant_id=tid,
            name=shop_name,
            phone=phone,
            bank_account=acc,
            created_at=start_date,
        )
        retailers.append(ret)

    # -----------------------------------------------------------------------
    # 2. Invoices (300)
    # -----------------------------------------------------------------------
    invoices: list[Invoice] = []
    # Distinct common amounts in santim (e.g. 5,000 ETB to 250,000 ETB)
    amount_pool = [
        1500000, 2400000, 3500000, 4800000, 6000000, 7500000, 9200000,
        12000000, 15500000, 18000000, 22000000, 28000000, 35000000, 45000000,
        50000000, 75000000, 100000000, 125000000, 180000000, 250000000
    ]

    for i in range(1, 301):
        ret = retailers[(i - 1) % len(retailers)]
        amt = rng.choice(amount_pool)
        issue_d = start_date + timedelta(days=rng.randint(0, 45))
        due_d = issue_d + timedelta(days=30)
        inv = Invoice(
            id=uuid.uuid4(),
            tenant_id=tid,
            invoice_number=f"INV-{i:03d}",
            retailer_id=ret.id,
            amount_santim=amt,
            issued_date=issue_d,
            due_date=due_d,
            state="open",
            created_at=issue_d,
            created_by=uid,
        )
        invoices.append(inv)

    # -----------------------------------------------------------------------
    # 3. Payment Lines (400) with Ground Truth & Noise Injection
    # -----------------------------------------------------------------------
    payments_with_truth: list[SyntheticPaymentGroundTruth] = []
    retailers_by_id = {r.id: r for r in retailers}

    # Track invoice usage to avoid accidental collisions in clean tiers
    used_invoices: set[uuid.UUID] = set()

    # (A) Clean Tier A Auto-Matches (170 payments): exact invoice number in ref, equal amount, L2 evidence
    for inv in invoices[:170]:
        used_invoices.add(inv.id)
        ret = retailers_by_id[inv.retailer_id]
        pay_date = inv.due_date - timedelta(days=rng.randint(0, 5))
        ref_templates = [
            f"Payment for {inv.invoice_number}",
            f"Ref: {inv.invoice_number} / {ret.name}",
            f"{inv.invoice_number} settlement",
            f"Transfer {inv.invoice_number}",
        ]
        pl = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=tid,
            source_ref=f"FT{rng.randint(100000, 999999)}",
            amount_santim=inv.amount_santim,
            value_date=pay_date,
            counterparty_name=ret.name,
            counterparty_account=ret.bank_account,
            raw_reference=rng.choice(ref_templates),
            evidence_level="L2",
            state="unmatched",
            created_at=pay_date,
        )
        payments_with_truth.append(
            SyntheticPaymentGroundTruth(
                payment_line=pl,
                expected_status="auto_confirmed",
                expected_invoice_ids=[inv.id],
                noise_type="clean_tier_a",
                is_auto_confirmable=True,
            )
        )

    # (B) Clean Tier B Auto-Matches (70 payments): missing/blank reference, but counterparty phone/acc matches, equal amount within date window, L2 evidence
    for inv in invoices[170:240]:
        used_invoices.add(inv.id)
        ret = retailers_by_id[inv.retailer_id]
        pay_date = inv.due_date + timedelta(days=rng.randint(-10, 10))
        generic_refs = ["CBE Birr Transfer", "Telebirr Cash In", "Bank Deposit", None, ""]
        pl = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=tid,
            source_ref=f"TXN{rng.randint(100000, 999999)}",
            amount_santim=inv.amount_santim,
            value_date=pay_date,
            counterparty_name=ret.name,
            counterparty_account=ret.phone if rng.random() > 0.5 else ret.bank_account,
            raw_reference=rng.choice(generic_refs),
            evidence_level="L2",
            state="unmatched",
            created_at=pay_date,
        )
        payments_with_truth.append(
            SyntheticPaymentGroundTruth(
                payment_line=pl,
                expected_status="auto_confirmed",
                expected_invoice_ids=[inv.id],
                noise_type="clean_tier_b",
                is_auto_confirmable=True,
            )
        )

    # (C) Tier C Transliterated Name Variations (30 payments): exact name transliteration variant, no invoice ref, equal amount, L2 evidence
    for inv in invoices[240:270]:
        used_invoices.add(inv.id)
        ret = retailers_by_id[inv.retailer_id]
        pay_date = inv.due_date
        # Apply transliteration change
        var_name = ret.name
        for orig, alt in TRANSLITERATION_MAP.items():
            if orig in var_name:
                var_name = var_name.replace(orig, alt)
                break

        pl = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=tid,
            source_ref=f"CBE{rng.randint(100000, 999999)}",
            amount_santim=inv.amount_santim,
            value_date=pay_date,
            counterparty_name=var_name,
            counterparty_account=None,
            raw_reference=f"Transfer from {var_name}",
            evidence_level="L2",
            state="unmatched",
            created_at=pay_date,
        )
        payments_with_truth.append(
            SyntheticPaymentGroundTruth(
                payment_line=pl,
                expected_status="auto_confirmed",
                expected_invoice_ids=[inv.id],
                noise_type="transliteration",
                is_auto_confirmable=True,
            )
        )

    # (D) Partial Payments (20 payments): amount strictly less than invoice, MUST BE PROPOSED ONLY
    for inv in invoices[270:290]:
        ret = retailers_by_id[inv.retailer_id]
        partial_amount = inv.amount_santim // 2
        pl = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=tid,
            source_ref=f"PART{rng.randint(10000, 99999)}",
            amount_santim=partial_amount,
            value_date=inv.due_date,
            counterparty_name=ret.name,
            counterparty_account=ret.phone,
            raw_reference=f"Part payment for {inv.invoice_number}",
            evidence_level="L2",
            state="unmatched",
            created_at=inv.due_date,
        )
        payments_with_truth.append(
            SyntheticPaymentGroundTruth(
                payment_line=pl,
                expected_status="proposed",  # Rule: Partial payments ONLY proposed!
                expected_invoice_ids=[inv.id],
                noise_type="partial_payment",
                is_auto_confirmable=False,
            )
        )

    # (E) Combined Payments (15 payments): single payment covers 2 invoices for same retailer, MUST BE PROPOSED ONLY
    for k in range(15):
        ret = retailers[k]
        ret_invs = [inv for inv in invoices if inv.retailer_id == ret.id]
        if len(ret_invs) >= 2:
            inv1, inv2 = ret_invs[0], ret_invs[1]
            comb_amt = inv1.amount_santim + inv2.amount_santim
            pl = PaymentLine(
                id=uuid.uuid4(),
                tenant_id=tid,
                source_ref=f"COMB{rng.randint(10000, 99999)}",
                amount_santim=comb_amt,
                value_date=inv1.due_date,
                counterparty_name=ret.name,
                counterparty_account=ret.bank_account,
                raw_reference=f"Combined settlement {inv1.invoice_number} & {inv2.invoice_number}",
                evidence_level="L2",
                state="unmatched",
                created_at=inv1.due_date,
            )
            payments_with_truth.append(
                SyntheticPaymentGroundTruth(
                    payment_line=pl,
                    expected_status="proposed",  # Rule: Combined payments ONLY proposed!
                    expected_invoice_ids=[inv1.id, inv2.id],
                    noise_type="combined_payment",
                    is_auto_confirmable=False,
                )
            )

    # (F) Same-Amount Collisions (20 payments): same retailer has multiple open invoices with identical amount!
    # MUST NOT auto-confirm — Rule 3: send to review queue!
    collision_ret = retailers[0]
    # Create 2 collision invoices for collision_ret with identical amount 8888000 santim
    col_amt = 8888000
    col_inv1 = Invoice(
        id=uuid.uuid4(),
        tenant_id=tid,
        invoice_number="INV-COL-001",
        retailer_id=collision_ret.id,
        amount_santim=col_amt,
        issued_date=start_date,
        due_date=start_date + timedelta(days=30),
        state="open",
        created_at=start_date,
        created_by=uid,
    )
    col_inv2 = Invoice(
        id=uuid.uuid4(),
        tenant_id=tid,
        invoice_number="INV-COL-002",
        retailer_id=collision_ret.id,
        amount_santim=col_amt,
        issued_date=start_date + timedelta(days=5),
        due_date=start_date + timedelta(days=35),
        state="open",
        created_at=start_date,
        created_by=uid,
    )
    invoices.append(col_inv1)
    invoices.append(col_inv2)

    for c in range(20):
        pl = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=tid,
            source_ref=f"COLL{c:03d}",
            amount_santim=col_amt,
            value_date=start_date + timedelta(days=30),
            counterparty_name=collision_ret.name,
            counterparty_account=collision_ret.phone,
            raw_reference="Deposit",  # No invoice number in reference!
            evidence_level="L2",
            state="unmatched",
            created_at=start_date + timedelta(days=30),
        )
        payments_with_truth.append(
            SyntheticPaymentGroundTruth(
                payment_line=pl,
                expected_status="proposed",  # Rule 3: Multiple plausible invoices -> Review Queue!
                expected_invoice_ids=[col_inv1.id, col_inv2.id],
                noise_type="same_amount_collision",
                is_auto_confirmable=False,
            )
        )

    # (G) L4 Evidence Payments (15 payments): screenshots/SMS notes with perfect invoice match
    # Rule 2: MUST NEVER BE AUTO-CONFIRMED! MUST BE PROPOSED ONLY!
    for inv in invoices[10:25]:
        ret = retailers_by_id[inv.retailer_id]
        pl = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=tid,
            source_ref="SMS_FWD_NOTE",
            amount_santim=inv.amount_santim,
            value_date=inv.due_date,
            counterparty_name=ret.name,
            counterparty_account=ret.phone,
            raw_reference=f"Photo of receipt for {inv.invoice_number}",
            evidence_level="L4",  # L4 evidence!
            state="unmatched",
            created_at=inv.due_date,
        )
        payments_with_truth.append(
            SyntheticPaymentGroundTruth(
                payment_line=pl,
                expected_status="proposed",  # Rule 2: L4 CAN NEVER BE CONFIRMED!
                expected_invoice_ids=[inv.id],
                noise_type="l4_evidence",
                is_auto_confirmable=False,
            )
        )

    # (H) Duplicate lines (15 payments): same payment line appearing a second time
    for item in payments_with_truth[:15]:
        orig = item.payment_line
        dup = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=tid,
            source_ref=orig.source_ref,
            amount_santim=orig.amount_santim,
            value_date=orig.value_date,
            counterparty_name=orig.counterparty_name,
            counterparty_account=orig.counterparty_account,
            raw_reference=orig.raw_reference,
            evidence_level=orig.evidence_level,
            state="unmatched",
            created_at=orig.created_at,
        )
        payments_with_truth.append(
            SyntheticPaymentGroundTruth(
                payment_line=dup,
                expected_status="proposed" if item.expected_status == "proposed" else "auto_confirmed",
                expected_invoice_ids=item.expected_invoice_ids,
                noise_type="duplicate",
                is_auto_confirmable=item.is_auto_confirmable,
            )
        )

    # (I) Date Delays (20 payments): payments made > 90 days after due date with no invoice reference
    # Outside the Tier B 60-day date window -> MUST NOT auto-confirm under Tier B!
    for inv in invoices[50:70]:
        ret = retailers_by_id[inv.retailer_id]
        delayed_date = inv.due_date + timedelta(days=120)  # 120 days late!
        pl = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=tid,
            source_ref=f"DELAY{rng.randint(1000, 9999)}",
            amount_santim=inv.amount_santim,
            value_date=delayed_date,
            counterparty_name=ret.name,
            counterparty_account=ret.phone,
            raw_reference="Bank transfer",  # No invoice number in reference!
            evidence_level="L2",
            state="unmatched",
            created_at=delayed_date,
        )
        payments_with_truth.append(
            SyntheticPaymentGroundTruth(
                payment_line=pl,
                expected_status="proposed",  # Beyond date window -> Proposed / review queue
                expected_invoice_ids=[inv.id],
                noise_type="date_delay",
                is_auto_confirmable=False,
            )
        )

    # (J) Unmatched Noise Lines (25 payments): random transfers from unknown buyers, zero matches
    for u in range(25):
        noise_amt = rng.randint(50000, 9999999)
        pl = PaymentLine(
            id=uuid.uuid4(),
            tenant_id=tid,
            source_ref=f"NOISE{u:04d}",
            amount_santim=noise_amt,
            value_date=start_date + timedelta(days=rng.randint(0, 60)),
            counterparty_name=f"Unknown Customer {u}",
            counterparty_account="0900000000",
            raw_reference="Personal transfer for goods",
            evidence_level="L2",
            state="unmatched",
            created_at=start_date,
        )
        payments_with_truth.append(
            SyntheticPaymentGroundTruth(
                payment_line=pl,
                expected_status="unmatched",
                expected_invoice_ids=[],
                noise_type="unmatched_noise",
                is_auto_confirmable=False,
            )
        )

    # Trim or ensure exactly 400 payment lines
    final_payments = payments_with_truth[:400]

    return SyntheticDataSet(
        tenant_id=tid,
        retailers=retailers,
        invoices=invoices,
        payments_with_truth=final_payments,
    )
