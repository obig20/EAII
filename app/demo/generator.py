"""
app/demo/generator.py

Deterministic generator for realistic Addis Ababa wholesale demo tenants.
Populates:
  - 12 Ethiopian retailers across major commercial hubs (Mercato, Piassa, Bole, etc.)
  - ~30 invoices (15k - 450k ETB) distributed across all 5 aging buckets
  - 1 imported statement file with 9 lines demonstrating all matching behaviors:
      * 3 Tier A exact matches (auto-confirmed)
      * 2 Tier B matches (phone/account match, review queue)
      * 1 Tier C match (fuzzy name match, review queue)
      * 1 Differing amount / partial payment (review queue, partial label)
      * 1 Overpayment with unallocated credit remainder (review queue, overpayment label)
      * 1 L4 cash receipt note ("Woizero Almaz brought cash", cannot auto-confirm)
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.kernel.auth import hash_password, normalize_identifier
from app.kernel.enums import (
    EvidenceLevel,
    InvoiceState,
    MatchState,
    MatchTier,
    PaymentState,
    UserRole,
)
from app.kernel.events import append_event
from app.kernel.models import (
    ImportBatch,
    Invoice,
    PaymentLine,
    PaymentMatch,
    Retailer,
    Tenant,
    User,
)
from app.kernel.tenant import set_tenant


RETAILER_SEEDS = [
    {
        "name": "Almaz & Sons Trading / አልማዝ እና ልጆቿ",
        "phone": "+251911234501",
        "bank_account": "1000200101",
        "location": "Mercato, Sebategna",
    },
    {
        "name": "Abebe Grain Wholesale / አበበ እህል ጅምላ",
        "phone": "+251911234502",
        "bank_account": "1000200102",
        "location": "Mercato, Bomb Tera",
    },
    {
        "name": "Berhan Stationery / ብርሃን ስቴሽነሪ",
        "phone": "+251912234503",
        "bank_account": "1000200103",
        "location": "Piassa, Church Street",
    },
    {
        "name": "Tadesse Building Materials / ታደሰ ህንፃ መሳሪያ",
        "phone": "+251912234504",
        "bank_account": "1000200104",
        "location": "Mercato, Military Tera",
    },
    {
        "name": "Tewodros Cosmetics / ቴዎድሮስ ኮስሞቲክስ",
        "phone": "+251922234505",
        "bank_account": "1000200105",
        "location": "Piassa, Arada",
    },
    {
        "name": "Selam General Store / ሰላም ሱቅ",
        "phone": "+251922234506",
        "bank_account": "1000200106",
        "location": "Bole, Medhanialem",
    },
    {
        "name": "Bole Supermarket / ቦሌ ሱፐርማርኬት",
        "phone": "+251930234507",
        "bank_account": "1000200107",
        "location": "Bole, Atlas",
    },
    {
        "name": "Kazanchis Mart / ካዛንቺስ ማርት",
        "phone": "+251930234508",
        "bank_account": "1000200108",
        "location": "Kazanchis",
    },
    {
        "name": "Lem Hotel Provisions / ለም ሆቴል ሸቀጣ ሸቀጥ",
        "phone": "+251911234509",
        "bank_account": "1000200109",
        "location": "Megenagna",
    },
    {
        "name": "Saris Food Stuffs / ሳሪስ የምግብ ሸቀጥ",
        "phone": "+251912234510",
        "bank_account": "1000200110",
        "location": "Saris, Adey Abeba",
    },
    {
        "name": "Lebu Industrial Supplies / ሌቡ የኢንዱስትሪ እቃዎች",
        "phone": "+251922234511",
        "bank_account": "1000200111",
        "location": "Lebu, Haile Garment",
    },
    {
        "name": "Kera Meats & Provisions / ቄራ ስጋ እና ሸቀጥ",
        "phone": "+251930234512",
        "bank_account": "1000200112",
        "location": "Kera",
    },
]


def seed_demo_data(
    session: Session,
    tenant: Tenant,
    owner_user: User,
    as_of_date: date | None = None,
) -> dict[str, Any]:
    """
    Populate a demo tenant with realistic Addis Ababa wholesale data.
    """
    today = as_of_date or date.today()
    now = datetime.now(timezone.utc)
    set_tenant(session, tenant.id)

    # 1. Create 12 Retailers
    retailers: list[Retailer] = []
    for data in RETAILER_SEEDS:
        r = Retailer(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            name=data["name"],
            phone=data["phone"],
            bank_account=data["bank_account"],
            created_at=now,
        )
        session.add(r)
        retailers.append(r)
    session.flush()

    # 2. Create 30 Invoices across all 5 Aging Buckets
    # Bucket definitions relative to today:
    # 1. Current (due in future: +10 to +25 days)
    # 2. 1-30 days overdue (due -5 to -25 days)
    # 3. 31-60 days overdue (due -35 to -55 days)
    # 4. 61-90 days overdue (due -65 to -85 days)
    # 5. 90+ days overdue (due -95 to -150 days)

    # Realistic wholesale amounts (santim): 15,000 ETB to 450,000 ETB
    invoice_specs = [
        # Current bucket (due in future) -> 6 invoices
        (0, 4500000, -10, 20),    # inv 1: 45,000 ETB (will be Tier A settled)
        (1, 8500000, -12, 18),    # inv 2: 85,000 ETB (will be Tier A settled)
        (2, 15000000, -15, 15),   # inv 3: 150,000 ETB (will be Tier A settled)
        (3, 21000000, -5, 25),    # inv 4: 210,000 ETB
        (4, 34000000, -8, 22),    # inv 5: 340,000 ETB
        (5, 18500000, -14, 16),   # inv 6: 185,000 ETB

        # 1-30 days overdue -> 6 invoices
        (1, 6000000, -35, -5),    # inv 7: 60,000 ETB (Tier B match target)
        (2, 3500000, -40, -10),   # inv 8: 35,000 ETB (Tier B match target)
        (0, 12500000, -45, -15),  # inv 9: 125,000 ETB (Tier C fuzzy match target)
        (3, 12000000, -48, -18),  # inv 10: 120,000 ETB (Partial payment target: pays 80k)
        (4, 4000000, -50, -20),   # inv 11: 40,000 ETB (Overpayment target: pays 55k)
        (5, 2500000, -52, -22),   # inv 12: 25,000 ETB (L4 cash receipt target)

        # 31-60 days overdue -> 6 invoices
        (6, 7500000, -75, -45),   # 75,000 ETB
        (7, 9800000, -80, -50),   # 98,000 ETB
        (8, 14200000, -85, -55),  # 142,000 ETB
        (9, 6200000, -72, -42),   # 62,000 ETB
        (10, 19000000, -78, -48), # 190,000 ETB
        (11, 28000000, -82, -52), # 280,000 ETB

        # 61-90 days overdue -> 6 invoices
        (6, 11500000, -105, -75), # 115,000 ETB
        (7, 16000000, -110, -80), # 160,000 ETB
        (8, 23000000, -115, -85), # 230,000 ETB
        (9, 8400000, -102, -72),  # 84,000 ETB
        (10, 31000000, -108, -78),# 310,000 ETB
        (11, 45000000, -112, -82),# 450,000 ETB (Largest ticket item: 450k)

        # 90+ days overdue -> 6 invoices
        (0, 17500000, -150, -120),# 175,000 ETB
        (1, 22500000, -165, -135),# 225,000 ETB
        (2, 13500000, -180, -150),# 135,000 ETB
        (3, 29000000, -140, -110),# 290,000 ETB
        (4, 19500000, -155, -125),# 195,000 ETB
        (5, 33000000, -170, -140),# 330,000 ETB
    ]

    invoices: list[Invoice] = []
    for idx, (ret_idx, amt, issue_offset, due_offset) in enumerate(invoice_specs, start=1):
        inv_num = f"INV-2026-{idx:03d}"
        inv_issued = today + timedelta(days=issue_offset)
        inv_due = today + timedelta(days=due_offset)

        # First 3 will be settled by the Tier A payments; others open
        initial_state = InvoiceState.OPEN.value
        if idx in (1, 2, 3):
            initial_state = InvoiceState.SETTLED.value

        inv = Invoice(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            invoice_number=inv_num,
            retailer_id=retailers[ret_idx].id,
            amount_santim=amt,
            issued_date=inv_issued,
            due_date=inv_due,
            state=initial_state,
            created_at=now,
            created_by=owner_user.id,
        )
        session.add(inv)
        invoices.append(inv)
    session.flush()

    # 3. Create 1 Imported Statement Batch (CBE Statement)
    batch = ImportBatch(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        source_type="bank_statement",
        evidence_level="L2",
        filename="CBE_Statement_AddisWholesale_Today.csv",
        file_hash="demo-file-hash-cbe-statement-2026",
        imported_by=owner_user.id,
        imported_at=now,
    )
    session.add(batch)
    session.flush()

    append_event(
        session,
        tenant_id=tenant.id,
        event_type="batch.imported",
        aggregate_type="import_batch",
        aggregate_id=batch.id,
        actor_id=owner_user.id,
        payload={"filename": batch.filename, "source_type": "bank_statement"},
    )

    # 4. Create Statement Payment Lines & Matches
    # Line 1: Tier A exact match (INV-2026-001, 45,000 ETB) -> Auto-confirmed
    pl1 = PaymentLine(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        import_batch_id=batch.id,
        source_ref="FT2610A101",
        row_key="demo-row-1",
        amount_santim=4500000,
        value_date=today - timedelta(days=1),
        counterparty_name="Almaz & Sons",
        raw_reference=f"FT2610A101 {invoices[0].invoice_number} TRF",
        evidence_level="L2",
        state=PaymentState.CONFIRMED.value,
        created_at=now,
    )
    pm1 = PaymentMatch(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        payment_line_id=pl1.id,
        invoice_id=invoices[0].id,
        matched_santim=4500000,
        tier=MatchTier.TIER_A.value,
        state=MatchState.CONFIRMED.value,
        confirmed_by=owner_user.id,
        confirmed_at=now,
        created_at=now,
    )

    # Line 2: Tier A exact match (INV-2026-002, 85,000 ETB) -> Auto-confirmed
    pl2 = PaymentLine(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        import_batch_id=batch.id,
        source_ref="CBE772102",
        row_key="demo-row-2",
        amount_santim=8500000,
        value_date=today - timedelta(days=1),
        counterparty_name="Abebe Grain",
        raw_reference=f"CBE*TX7721 {invoices[1].invoice_number}",
        evidence_level="L2",
        state=PaymentState.CONFIRMED.value,
        created_at=now,
    )
    pm2 = PaymentMatch(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        payment_line_id=pl2.id,
        invoice_id=invoices[1].id,
        matched_santim=8500000,
        tier=MatchTier.TIER_A.value,
        state=MatchState.CONFIRMED.value,
        confirmed_by=owner_user.id,
        confirmed_at=now,
        created_at=now,
    )

    # Line 3: Tier A exact match (INV-2026-003, 150,000 ETB) -> Auto-confirmed
    pl3 = PaymentLine(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        import_batch_id=batch.id,
        source_ref="TB991203",
        row_key="demo-row-3",
        amount_santim=15000000,
        value_date=today - timedelta(days=1),
        counterparty_name="Berhan Stationery",
        raw_reference=f"TELEBIRR {invoices[2].invoice_number} DIRECT",
        evidence_level="L2",
        state=PaymentState.CONFIRMED.value,
        created_at=now,
    )
    pm3 = PaymentMatch(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        payment_line_id=pl3.id,
        invoice_id=invoices[2].id,
        matched_santim=15000000,
        tier=MatchTier.TIER_A.value,
        state=MatchState.CONFIRMED.value,
        confirmed_by=owner_user.id,
        confirmed_at=now,
        created_at=now,
    )

    # Line 4: Tier B phone/account match (INV-2026-007, 60,000 ETB) -> Review Queue
    pl4 = PaymentLine(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        import_batch_id=batch.id,
        source_ref="FT2610B104",
        row_key="demo-row-4",
        amount_santim=6000000,
        value_date=today - timedelta(days=2),
        counterparty_name="Abebe Grain Wholesale",
        counterparty_account=retailers[1].bank_account,
        raw_reference=f"Transfer from mobile {retailers[1].phone}",
        evidence_level="L2",
        state=PaymentState.PROPOSED.value,
        created_at=now,
    )
    pm4 = PaymentMatch(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        payment_line_id=pl4.id,
        invoice_id=invoices[6].id,
        matched_santim=6000000,
        tier=MatchTier.TIER_B.value,
        state=MatchState.PROPOSED.value,
        created_at=now,
    )

    # Line 5: Tier B account match (INV-2026-008, 35,000 ETB) -> Review Queue
    pl5 = PaymentLine(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        import_batch_id=batch.id,
        source_ref="FT2610B105",
        row_key="demo-row-5",
        amount_santim=3500000,
        value_date=today - timedelta(days=2),
        counterparty_name="Berhan Stationery",
        counterparty_account=retailers[2].bank_account,
        raw_reference=f"CBE Birr from acct {retailers[2].bank_account}",
        evidence_level="L2",
        state=PaymentState.PROPOSED.value,
        created_at=now,
    )
    pm5 = PaymentMatch(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        payment_line_id=pl5.id,
        invoice_id=invoices[7].id,
        matched_santim=3500000,
        tier=MatchTier.TIER_B.value,
        state=MatchState.PROPOSED.value,
        created_at=now,
    )

    # Line 6: Tier C fuzzy name match (INV-2026-009, 125,000 ETB) -> Review Queue
    pl6 = PaymentLine(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        import_batch_id=batch.id,
        source_ref="FT2610C106",
        row_key="demo-row-6",
        amount_santim=12500000,
        value_date=today - timedelta(days=3),
        counterparty_name="Almaz Wholesalers",
        raw_reference="Direct Bank Transfer Addis Ababa",
        evidence_level="L2",
        state=PaymentState.PROPOSED.value,
        created_at=now,
    )
    pm6 = PaymentMatch(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        payment_line_id=pl6.id,
        invoice_id=invoices[8].id,
        matched_santim=12500000,
        tier=MatchTier.TIER_C.value,
        state=MatchState.PROPOSED.value,
        created_at=now,
    )

    # Line 7: Differing amount / Partial payment (INV-2026-010 open 120,000 ETB, paying 80,000 ETB)
    pl7 = PaymentLine(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        import_batch_id=batch.id,
        source_ref="FT2610PART07",
        row_key="demo-row-7",
        amount_santim=8000000,
        value_date=today - timedelta(days=1),
        counterparty_name="Tadesse Building Materials",
        counterparty_account=retailers[3].bank_account,
        raw_reference=f"Partial settlement for {invoices[9].invoice_number}",
        evidence_level="L2",
        state=PaymentState.PROPOSED.value,
        created_at=now,
    )
    pm7 = PaymentMatch(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        payment_line_id=pl7.id,
        invoice_id=invoices[9].id,
        matched_santim=8000000,
        tier=MatchTier.TIER_A.value,
        state=MatchState.PROPOSED.value,
        created_at=now,
    )

    # Line 8: Overpayment with credit remainder (INV-2026-011 open 40,000 ETB, paying 55,000 ETB)
    pl8 = PaymentLine(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        import_batch_id=batch.id,
        source_ref="FT2610OVER08",
        row_key="demo-row-8",
        amount_santim=5500000,
        value_date=today - timedelta(days=1),
        counterparty_name="Tewodros Cosmetics",
        counterparty_account=retailers[4].bank_account,
        raw_reference=f"Settlement & advance deposit for {invoices[10].invoice_number}",
        evidence_level="L2",
        state=PaymentState.PROPOSED.value,
        created_at=now,
    )
    pm8 = PaymentMatch(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        payment_line_id=pl8.id,
        invoice_id=invoices[10].id,
        matched_santim=4000000,
        tier=MatchTier.TIER_A.value,
        state=MatchState.PROPOSED.value,
        created_at=now,
    )

    # Line 9: L4 receipt note ("Woizero Almaz brought cash", cannot auto-confirm)
    pl9 = PaymentLine(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        import_batch_id=batch.id,
        source_ref="CASH-REC-4891",
        row_key="demo-row-9",
        amount_santim=2500000,
        value_date=today,
        counterparty_name="Almaz & Sons Trading",
        raw_reference="Cash receipt #4891: Woizero Almaz brought cash to office 25,000 ETB",
        evidence_level="L4",
        state=PaymentState.PROPOSED.value,
        created_at=now,
    )
    pm9 = PaymentMatch(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        payment_line_id=pl9.id,
        invoice_id=invoices[11].id,
        matched_santim=2500000,
        tier=MatchTier.TIER_C.value,
        state=MatchState.PROPOSED.value,
        created_at=now,
    )

    all_lines = [pl1, pl2, pl3, pl4, pl5, pl6, pl7, pl8, pl9]
    all_matches = [pm1, pm2, pm3, pm4, pm5, pm6, pm7, pm8, pm9]
    session.add_all(all_lines)
    session.add_all(all_matches)
    session.flush()

    # Log auto-confirm events for Tier A lines
    for pm, inv, pl in [(pm1, invoices[0], pl1), (pm2, invoices[1], pl2), (pm3, invoices[2], pl3)]:
        append_event(
            session,
            tenant_id=tenant.id,
            event_type="match.auto_confirmed",
            aggregate_type="payment_match",
            aggregate_id=pm.id,
            actor_id=owner_user.id,
            payload={
                "invoice_id": str(inv.id),
                "payment_line_id": str(pl.id),
                "matched_santim": pm.matched_santim,
                "tier": "tier_a",
            },
        )

    session.flush()
    return {
        "tenant": tenant,
        "owner": owner_user,
        "retailers": retailers,
        "invoices": invoices,
        "batch": batch,
        "payment_lines": all_lines,
        "payment_matches": all_matches,
    }


def create_demo_tenant(
    session: Session,
    tenant_name: str = "Addis Wholesale Demo / አዲስ ጅምላ ማሳያ",
    owner_identifier: str = "demo@dailyclose.et",
    owner_name: str = "Ato Demo Owner",
    owner_password: str = "DemoDailyClose2026!",
) -> tuple[Tenant, User]:
    """
    Create a fresh Demo tenant and seed it with realistic Addis wholesale data.
    """
    clean_id = normalize_identifier(owner_identifier)
    email = clean_id if "@" in clean_id else None
    phone = clean_id if "@" not in clean_id else None
    now = datetime.now(timezone.utc)

    # 1. Create Tenant with is_demo=True
    tenant = Tenant(
        id=uuid.uuid4(),
        name=tenant_name,
        is_demo=True,
        created_at=now,
    )
    session.add(tenant)
    session.flush()
    set_tenant(session, tenant.id)

    # 2. Create Owner User
    owner = User(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        role=UserRole.OWNER.value,
        display_name=owner_name,
        email=email,
        phone=phone,
        password_hash=hash_password(owner_password),
        is_active=True,
        created_at=now,
    )
    session.add(owner)
    session.flush()

    append_event(
        session,
        tenant_id=tenant.id,
        event_type="auth.user_created",
        aggregate_type="user",
        aggregate_id=owner.id,
        actor_id=owner.id,
        payload={"role": "owner", "demo_bootstrap": True},
    )

    # 3. Seed Demo Data
    seed_demo_data(session, tenant, owner)
    session.commit()
    return tenant, owner
