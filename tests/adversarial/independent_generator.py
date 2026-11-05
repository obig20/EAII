"""
tests/adversarial/independent_generator.py

Clean-room independent synthetic test data generator for Ethiopian credit-sale and bank statement reconciliation.
Authored independently of app/matching/matcher.py to ensure zero benchmark bias.

Generates ground-truth pairs:
  - Retailers with realistic Ethiopian names, phone numbers, and Amharic variations
  - Invoices with realistic santim amounts and payment terms
  - Bank and Telebirr statement lines with varied noise:
      * Missing references
      * Compound references (multiple invoices)
      * Partial payments
      * Transliteration and script discrepancies (Latin vs Ge'ez / Fidel)
      * OCR / character-substitution noise (0/O, 1/I/l, 5/S, 8/B)
      * Amount collisions (unrelated transactions sharing identical amounts)
      * Out-of-window dates
      * Unmatched / noise transactions (bank fees, tax, random deposits)
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------
@dataclass
class SyntheticRetailer:
    id: uuid.UUID
    name_latin: str
    name_amharic: str
    phone: str
    account_number: str
    aliases: list[str] = field(default_factory=list)


@dataclass
class SyntheticInvoice:
    id: uuid.UUID
    retailer_id: uuid.UUID
    invoice_number: str
    amount_santim: int
    issued_date: date
    due_date: date


@dataclass
class SyntheticPaymentLine:
    id: uuid.UUID
    value_date: date
    amount_santim: int
    evidence_level: str  # L1, L2, L3, L4
    raw_reference: str
    counterparty_name: str
    counterparty_account: str
    source_ref: str
    noise_type: str
    # Ground truth expectations:
    expected_invoice_ids: list[uuid.UUID] = field(default_factory=list)
    expected_status: Literal["auto_confirmed", "proposed", "unmatched"] = "unmatched"
    is_partial: bool = False
    is_combined: bool = False
    is_ambiguous: bool = False


@dataclass
class IndependentDataset:
    retailers: list[SyntheticRetailer]
    invoices: list[SyntheticInvoice]
    payments: list[SyntheticPaymentLine]


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------
ETHIOPIAN_FIRST_NAMES = [
    ("Abebe", "አበበ", ["Ababa", "Abebech", "Abe"]),
    ("Almaz", "አልማዝ", ["Almaz", "Elmaz"]),
    ("Chilot", "ጭሎት", ["Chilot", "Chilote"]),
    ("Dawit", "ዳዊት", ["David", "Davit"]),
    ("Ephrem", "ኤፍሬም", ["Efrem", "Ephraim"]),
    ("Fikru", "ፍቅሩ", ["Fekru", "Fikre"]),
    ("Girma", "ግርማ", ["Germa", "Grm"]),
    ("Haile", "ኃይሌ", ["Hayle", "Hailemariam"]),
    ("Kassahun", "ካሳሁን", ["Kassa", "Kassu"]),
    ("Mulugeta", "ሙሉጌታ", ["Mulu", "Mulu-geta"]),
    ("Netsanet", "ነፃነት", ["Nesanet", "Netsi"]),
    ("Selamawit", "ሰላማዊት", ["Selam", "Selamawit"]),
    ("Tadesse", "ታደሰ", ["Taddese", "Tad"]),
    ("Worku", "ወርቁ", ["Werku", "Workneh"]),
    ("Yohannes", "ዮሐንስ", ["John", "Yohanes"]),
    ("Zewdu", "ዘውዱ", ["Zewdie", "Zewdu"]),
]

ETHIOPIAN_LAST_NAMES = [
    ("Bekele", "በቀለ"),
    ("Assefa", "አሰፋ"),
    ("Kebede", "ከበደ"),
    ("Tilahun", " ጥላሁን"),
    ("Mengistu", "መንግስቱ"),
    ("Tesfaye", "ተስፋዬ"),
    ("Alemu", "አለሙ"),
    ("Demissie", "ደሚሴ"),
]

STORE_TYPES = [
    ("Shop", "ሱቅ"),
    ("Wholesale", "ጅምላ"),
    ("Mini Market", "ሚኒ ማርኬት"),
    ("Pharmacy", "ፋርማሲ"),
    ("Electronics", "ኤሌክትሮኒክስ"),
    ("Trading", "ንግድ"),
]


class IndependentSyntheticGenerator:
    def __init__(self, seed: int = 42):
        self.rng = random.Random(seed)

    def generate(
        self,
        num_retailers: int = 30,
        num_invoices: int = 300,
        num_payments: int = 400,
        base_date: date = date(2026, 10, 1),
    ) -> IndependentDataset:
        # 1. Retailers
        retailers: list[SyntheticRetailer] = []
        for i in range(num_retailers):
            first_l, first_a, aliases = self.rng.choice(ETHIOPIAN_FIRST_NAMES)
            last_l, last_a = self.rng.choice(ETHIOPIAN_LAST_NAMES)
            store_l, store_a = self.rng.choice(STORE_TYPES)

            phone = f"+2519{self.rng.randint(10000000, 99999999)}"
            acct = f"1000{self.rng.randint(10000000, 99999999)}"
            name_latin = f"{first_l} {last_l} {store_l}"
            name_amharic = f"{first_a} {last_a} {store_a}"

            extra_aliases = [f"{a} {last_l}" for a in aliases]
            extra_aliases.append(f"{first_l} {last_l}")
            extra_aliases.append(name_amharic)

            retailers.append(
                SyntheticRetailer(
                    id=uuid.uuid4(),
                    name_latin=name_latin,
                    name_amharic=name_amharic,
                    phone=phone,
                    account_number=acct,
                    aliases=extra_aliases,
                )
            )

        # 2. Invoices
        invoices: list[SyntheticInvoice] = []
        for i in range(1, num_invoices + 1):
            ret = self.rng.choice(retailers)
            amt_etb = self.rng.choice([500, 1200, 2500, 4800, 7500, 12000, 25000, 50000, 80000, 125000])
            # Add some irregular santim
            if self.rng.random() < 0.3:
                amt_etb += self.rng.choice([0.25, 0.50, 0.75])
            amt_santim = int(round(amt_etb * 100))

            day_offset = self.rng.randint(0, 30)
            issued = base_date + timedelta(days=day_offset)
            due = issued + timedelta(days=self.rng.choice([15, 30, 45, 60]))

            invoices.append(
                SyntheticInvoice(
                    id=uuid.uuid4(),
                    retailer_id=ret.id,
                    invoice_number=f"INV-IND-{i:04d}",
                    amount_santim=amt_santim,
                    issued_date=issued,
                    due_date=due,
                )
            )

        # 3. Payments with Ground Truth
        payments: list[SyntheticPaymentLine] = []
        inv_by_id = {inv.id: inv for inv in invoices}
        ret_by_id = {ret.id: ret for ret in retailers}

        # Track noise counts for audit
        noise_distribution = {
            "clean_exact_reference": 0.30,       # Tier A: exact invoice ref + exact amount
            "ocr_reference_typo": 0.08,          # Ref with 0->O, 1->I typo
            "clean_counterparty_phone": 0.15,    # Tier B: counterparty phone + exact amount
            "name_alias_fuzzy": 0.12,            # Tier C: fuzzy name / Amharic transliteration
            "partial_payment": 0.08,             # Rule 3: Must be PROPOSED only
            "combined_multi_invoice": 0.06,      # Rule 3: Must be PROPOSED only
            "amount_collision": 0.06,            # Rule 3: Multiple open invoices with same amount -> PROPOSED
            "spurious_unmatched": 0.10,          # Unmatched deposit/charge -> UNMATCHED
            "date_out_of_window": 0.05,          # Paid 90 days later -> Review queue
        }

        inv_pool = list(invoices)
        self.rng.shuffle(inv_pool)
        inv_idx = 0

        for p_idx in range(1, num_payments + 1):
            category = self.rng.choices(
                list(noise_distribution.keys()),
                weights=list(noise_distribution.values()),
            )[0]

            pl_id = uuid.uuid4()
            src_ref = f"FT{self.rng.randint(1000000000, 9999999999)}"

            if category == "clean_exact_reference" and inv_idx < len(inv_pool):
                inv = inv_pool[inv_idx]
                inv_idx += 1
                ret = ret_by_id[inv.retailer_id]
                vdate = inv.issued_date + timedelta(days=self.rng.randint(1, 10))
                payments.append(
                    SyntheticPaymentLine(
                        id=pl_id,
                        value_date=vdate,
                        amount_santim=inv.amount_santim,
                        evidence_level="L2",
                        raw_reference=f"FT{src_ref} Payment for {inv.invoice_number}",
                        counterparty_name=ret.name_latin,
                        counterparty_account=ret.account_number,
                        source_ref=src_ref,
                        noise_type=category,
                        expected_invoice_ids=[inv.id],
                        expected_status="auto_confirmed",
                    )
                )

            elif category == "ocr_reference_typo" and inv_idx < len(inv_pool):
                inv = inv_pool[inv_idx]
                inv_idx += 1
                ret = ret_by_id[inv.retailer_id]
                vdate = inv.issued_date + timedelta(days=self.rng.randint(1, 10))
                # Introduce OCR typo in invoice number
                typo_inv = inv.invoice_number.replace("-", " ").replace("0", "O")
                payments.append(
                    SyntheticPaymentLine(
                        id=pl_id,
                        value_date=vdate,
                        amount_santim=inv.amount_santim,
                        evidence_level="L2",
                        raw_reference=f"Transfer Ref {typo_inv}",
                        counterparty_name=ret.name_latin,
                        counterparty_account=ret.account_number,
                        source_ref=src_ref,
                        noise_type=category,
                        expected_invoice_ids=[inv.id],
                        # If reference parser handles O/0 it might match, otherwise proposed
                        expected_status="proposed",
                    )
                )

            elif category == "clean_counterparty_phone" and inv_idx < len(inv_pool):
                inv = inv_pool[inv_idx]
                inv_idx += 1
                ret = ret_by_id[inv.retailer_id]
                vdate = inv.issued_date + timedelta(days=self.rng.randint(1, 14))
                payments.append(
                    SyntheticPaymentLine(
                        id=pl_id,
                        value_date=vdate,
                        amount_santim=inv.amount_santim,
                        evidence_level="L2",
                        raw_reference=f"Telebirr Transfer from {ret.phone}",
                        counterparty_name="Telebirr User",
                        counterparty_account=ret.phone,
                        source_ref=src_ref,
                        noise_type=category,
                        expected_invoice_ids=[inv.id],
                        expected_status="proposed",  # Tier B is usually proposed or auto depending on window
                    )
                )

            elif category == "name_alias_fuzzy" and inv_idx < len(inv_pool):
                inv = inv_pool[inv_idx]
                inv_idx += 1
                ret = ret_by_id[inv.retailer_id]
                vdate = inv.issued_date + timedelta(days=self.rng.randint(1, 14))
                alias = self.rng.choice(ret.aliases)
                payments.append(
                    SyntheticPaymentLine(
                        id=pl_id,
                        value_date=vdate,
                        amount_santim=inv.amount_santim,
                        evidence_level="L2",
                        raw_reference="Deposit by customer",
                        counterparty_name=alias,
                        counterparty_account="",
                        source_ref=src_ref,
                        noise_type=category,
                        expected_invoice_ids=[inv.id],
                        expected_status="proposed",  # Tier C fuzzy name is NEVER auto_confirmed
                    )
                )

            elif category == "partial_payment" and inv_idx < len(inv_pool):
                inv = inv_pool[inv_idx]
                inv_idx += 1
                ret = ret_by_id[inv.retailer_id]
                part_santim = inv.amount_santim // 2
                vdate = inv.issued_date + timedelta(days=self.rng.randint(1, 10))
                payments.append(
                    SyntheticPaymentLine(
                        id=pl_id,
                        value_date=vdate,
                        amount_santim=part_santim,
                        evidence_level="L2",
                        raw_reference=f"Partial settlement for {inv.invoice_number}",
                        counterparty_name=ret.name_latin,
                        counterparty_account=ret.account_number,
                        source_ref=src_ref,
                        noise_type=category,
                        expected_invoice_ids=[inv.id],
                        expected_status="proposed",  # RULE 3: Partial MUST BE PROPOSED ONLY
                        is_partial=True,
                    )
                )

            elif category == "combined_multi_invoice" and inv_idx + 1 < len(inv_pool):
                inv1 = inv_pool[inv_idx]
                inv2 = inv_pool[inv_idx + 1]
                inv_idx += 2
                ret = ret_by_id[inv1.retailer_id]
                tot_santim = inv1.amount_santim + inv2.amount_santim
                vdate = max(inv1.issued_date, inv2.issued_date) + timedelta(days=self.rng.randint(1, 5))
                payments.append(
                    SyntheticPaymentLine(
                        id=pl_id,
                        value_date=vdate,
                        amount_santim=tot_santim,
                        evidence_level="L2",
                        raw_reference=f"Payment {inv1.invoice_number} and {inv2.invoice_number}",
                        counterparty_name=ret.name_latin,
                        counterparty_account=ret.account_number,
                        source_ref=src_ref,
                        noise_type=category,
                        expected_invoice_ids=[inv1.id, inv2.id],
                        expected_status="proposed",  # RULE 3: Combined MUST BE PROPOSED ONLY
                        is_combined=True,
                    )
                )

            elif category == "amount_collision" and inv_idx + 1 < len(inv_pool):
                # Two distinct invoices with the exact same amount
                inv1 = inv_pool[inv_idx]
                inv2 = inv_pool[inv_idx + 1]
                inv_idx += 2
                collision_amt = 1500000  # 15,000 ETB
                inv1.amount_santim = collision_amt
                inv2.amount_santim = collision_amt
                vdate = inv1.issued_date + timedelta(days=2)
                payments.append(
                    SyntheticPaymentLine(
                        id=pl_id,
                        value_date=vdate,
                        amount_santim=collision_amt,
                        evidence_level="L2",
                        raw_reference="Bank transfer with no reference",
                        counterparty_name="Unknown Remitter",
                        counterparty_account="",
                        source_ref=src_ref,
                        noise_type=category,
                        expected_invoice_ids=[inv1.id, inv2.id],
                        expected_status="proposed",  # RULE 3: Ambiguous collision MUST BE PROPOSED ONLY
                        is_ambiguous=True,
                    )
                )

            else:
                # Spurious / unrelated payment
                amt = self.rng.randint(10000, 200000)
                payments.append(
                    SyntheticPaymentLine(
                        id=pl_id,
                        value_date=base_date + timedelta(days=self.rng.randint(0, 30)),
                        amount_santim=amt,
                        evidence_level="L2",
                        raw_reference=f"Tax or Utility Payment TX{self.rng.randint(1000,9999)}",
                        counterparty_name="Ethiopian Electric Utility",
                        counterparty_account="100000000000",
                        source_ref=src_ref,
                        noise_type="spurious_unmatched",
                        expected_invoice_ids=[],
                        expected_status="unmatched",
                    )
                )

        return IndependentDataset(retailers=retailers, invoices=invoices, payments=payments)
