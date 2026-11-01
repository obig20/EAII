# Daily Close (ዕለታዊ ሂሳብ ማጠቃለያ) — 2-Minute Demo Script

A guided walkthrough for wholesale owners, cashiers, and stakeholders demonstrating end-to-end daily reconciliation, evidence ladder controls, dispute/credit handling, and seamless bilingual English/Amharic navigation.

---

## ⏱️ Walkthrough Overview (2 Minutes)

| Step | Time | Screen / Feature | Key Action | Expected Result & Verification |
|------|------|------------------|------------|--------------------------------|
| **1** | 0:00 - 0:20 | `/login` → `/dashboard` | Sign in as Ato Demo Owner | 3 KPI summary cards; 6 matches in review queue; overdue-sorted aging report |
| **2** | 0:20 - 0:40 | Review Queue | Confirm Exact Bank Match (Abebe Grain) | Instant match confirmation; queue counter decrements to 5 |
| **3** | 0:40 - 1:00 | Review Queue | Confirm Differing Overpayment (Tewodros) | Shows 15,000.00 ETB kept on file as credit; queue decrements to 4 |
| **4** | 1:00 - 1:15 | Review Queue | Reject Match (Berhan Stationery) | Safe reject with confirmation; payment preserved in "Needs Attention" |
| **5** | 1:15 - 1:30 | Review Queue | Accept Unverified L4 Receipt (Almaz) | Rule 2 intact: marked unverified, never counted as paid |
| **6** | 1:30 - 1:45 | Language Switcher | Click "አማርኛ" (`/set-lang/am`) | Full Ge'ez interface: KPIs, checklists, queue, and aging table in Amharic |
| **7** | 1:45 - 2:00 | Dashboard | Reversal & Needs Attention Review | Recently confirmed match reversal available; audit trail logged |

---

## 🎬 Step-by-Step Execution Guide

### Step 1: Sign In as Wholesale Owner (0:00)
1. Navigate to `http://localhost:8000/login`.
2. Enter owner credentials:
   - **Identifier (Email / Phone):** `demo@dailyclose.et`
   - **Password:** `DemoDailyClose2026!`
3. Click **Sign In**.
4. **Expected UI Response:**
   - Redirected to `/dashboard`.
   - **Top KPI Cards:**
     - *Total Unpaid:* Displays aggregate debtor balance across all wholesale accounts.
     - *Overdue:* Displays debt exceeding payment terms in high-contrast red warning styling.
     - *Pending Matches:* Displays `6` suggested matches awaiting owner decision.
   - **Checklist:** Three one-tap action cards:
     - 1. Record Today's Credit Sales
     - 2. Import Today's Statement
     - 3. Approve Proposed Matches (shows badge `6`)

---

### Step 2: Confirm Routine Suggested Match (0:20)
1. Scroll to the **Review Queue** section.
2. Observe Card 1: **Abebe Grain Wholesale**:
   - Suggested match: Invoice `INV-2026-007` (60,000.00 ETB) matched against bank transfer of 60,000.00 ETB.
   - Evidence Level: `L2` (Confirmed by payment provider).
3. Click **Confirm Match**.
4. **Expected UI Response:**
   - Card updates immediately via HTMX swap to a green confirmation banner:
     `✓ Match Confirmed (ክፍያ ተረጋግጧል)`
   - Pending review queue counter updates dynamically to `5`.

---

### Step 3: Confirm Differing Overpayment with Credit on File (0:40)
1. Observe the card for **Tewodros Cosmetics**:
   - Payment Line: 55,000.00 ETB (`FT2610OVER08`).
   - Open Invoice `INV-2026-011`: 40,000.00 ETB.
   - Difference callout: `Amounts differ by 15,000.00 ETB (Overpayment)`.
   - Explicit credit notice: `Unallocated Credit: 15,000.00 ETB (kept on file as credit, not applied to other invoices)`.
2. Click **Confirm Differing Match (+15,000.00 ETB)**.
3. Native confirmation dialog prompts:
   ```text
   Confirm and keep 15,000.00 ETB as credit?
   
   • Retailer: Tewodros Cosmetics
   • Invoice: INV-2026-011
   • Raw Reference: Settlement & advance deposit for INV-2026-011
   
   Remaining amount will be kept on file as credit and not applied to other invoices.
   ```
4. Click **OK** (or Cancel to test request suppression).
5. **Expected UI Response:**
   - Invoice `INV-2026-011` is marked fully paid (40,000.00 ETB).
   - 15,000.00 ETB is recorded as unallocated credit balance for Tewodros Cosmetics.
   - Review queue counter decrements to `4`.

---

### Step 4: Reject Match & Retain in Audit Records (1:00)
1. Locate the card for **Berhan Stationery** (or fuzzy name candidate).
2. Click **Not this one (Reject Match)**.
3. Native confirmation dialog prompts:
   ```text
   Reject this suggested match? The payment line will remain in audit records.
   ```
4. Click **OK**.
5. **Expected UI Response:**
   - Card displays: `✕ Match Rejected`.
   - The payment line is **never deleted**. It is retained in audit records and moves to the **Needs Attention** list under the `Rejected` category.

---

### Step 5: Mark Unverified Photo / Cash Receipt Note (1:15)
1. Observe the card for **Almaz & Sons Trading** (`CASH-REC-4891`, 25,000.00 ETB):
   - Reference: `Cash receipt #4891: Woizero Almaz brought cash to office 25,000 ETB`.
   - Evidence Level: `L4` (Unverified manual receipt note).
   - Routine "Confirm Match" button is **strictly hidden** (Rule 2 Invariant).
2. Click **Accept as unverified (not counted as paid)**.
3. **Expected UI Response:**
   - Card transitions to unverified status banner:
     `✓ Marked as accepted_unverified (L4). Note: This amount is NOT counted as paid in any balance or report.`
   - Payment line moves to the **Needs Attention** section under `Accepted Unverified (L4)`.

---

### Step 6: Seamless Language Switch to Amharic (1:30)
1. In the top navigation header, click **አማርኛ**.
2. Endpoint `/set-lang/am` sets `lang=am` cookie and redirects to `/dashboard`.
3. **Expected UI Response:**
   - Fully translated Amharic interface:
     - Top summary cards:
       - `ያልተከፈለ አጠቃላይ` (Total Unpaid)
       - `ቀኑ ያለፈበት` (Overdue)
       - `ያልተረጋገጡ ክፍያዎች` (Pending Matches)
     - Today's Checklist:
       - `የዛሬው የሂሳብ ማጠቃለያ ስራዎች`
       - `1. የዛሬውን የብድር ሽያጭ ይመዝግቡ`
       - `2. የባንክ ወይም የቴሌብር መግለጫ ያስገቡ`
       - `3. የቀረቡ ክፍያዎችን ይገምግሙ`
     - Needs Attention section:
       - `ትኩረት የሚሹ ክፍያዎች` with category badges (`ውድቅ የተደረጉ`, `ያልተዛመዱ`, `ያልተረጋገጡ`, `የተመለሱ`).
     - AR Aging Table:
       - Columns: `ደንበኛ` (Retailer), `ያልተከፈለ ድምር` (Total Unpaid), `ያለፈበት` (Overdue), `የክፍያ ሁኔታ` (Terms).
       - Sorted with highest overdue debtors at the top.

---

### Step 7: Reversibility & Audit Trail (1:45)
1. Scroll to **በቅርብ የተረጋገጡ ክፍያዎች (መመለሻ)** (Recently Confirmed Matches).
2. Locate the confirmed match from Step 2.
3. Click **Reverse Match** (ቀደም ሲል የተረጋገጠ ማዛመጃ መመለሻ).
4. **Expected UI Response:**
   - Reversal confirmation dialog asks for confirmation.
   - On confirmation, the match transitions to `reversed`, restored open balance is applied back to the invoice, and a compensating event `match.reversed` is recorded in the immutable audit log.
   - The payment line returns to review / needs attention with flag `Previously Reversed` (`ቀደም ሲል የተመለሰ`).

---

## 🔒 Safety & Regulatory Compliance Notes
- **Evidence Ladder Rule 2:** L4 screenshot / photo receipts can never reduce debtor balances or auto-confirm.
- **Append-Only Immutability:** Event log is strictly append-only; database permissions forbid `UPDATE` or `DELETE` on the `events` table.
- **No Floating Point:** All math executes strictly in integer `santim` (1 ETB = 100 santim).
- **Zero AI / External API:** All matching and reconciliation execute deterministically on-premise in Addis Ababa.
