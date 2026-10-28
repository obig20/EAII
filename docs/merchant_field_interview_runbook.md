# Merchant Field Interview Runbook & Demonstration Playbook
**Birana Technology — Daily Close ERP Suite**  
*Addis Ababa, Ethiopia · የጅምላ ነጋዴዎች የመስክ ቃለ-መጠይቅ እና የ15-ደቂቃ የቀጥታ ማሳያ መመሪያ*

---

## 1. Executive Summary & Objective

This runbook is the tactical operating manual for commercial and engineering field interviews with wholesale merchants in major commercial districts of Addis Ababa (Merkato, Sebategna, Mesalemia, Kaliti Industrial Zone, and Bole Bulbula). 

The objective is twofold:
1. **Validate Merchant Pain Points:** Diagnose how mid-tier and large wholesale operations reconcile credit sales, bank deposits, multi-warehouse stock dispatches, cash collections from drivers, and monthly tax declarations.
2. **Execute a 15-Minute Compelling Live Demo:** Prove that Daily Close solves their #1 anxiety (cash leakage and uncollected receivables) within minutes—using realistic Ethiopian market scenarios (CBE, Telebirr, Dashen, Isuzu route deliveries, and Peachtree export).

---

## 2. 10-Second Collections Health Check Pitch Guide

When approaching an owner (*Ato / Weizero*) in their office or warehouse shopfront, merchants have zero patience for generic IT pitches. Deliver this precise 10-second diagnostic hook:

> **English Hook:**  
> *"Ato [Name], how much credit money is currently on the road with your delivery drivers and sitting uncollected in Telebirr and CBE without an invoice number? If we give you a CBE or Telebirr statement right now, Daily Close matches every single deposit to the right kiosk and warns you about uncollected cash before your drivers head home today."*

> **Amharic Hook (የአማርኛ አቀራረብ):**  
> *"አቶ [ስም]፣ አሁን በስርጭት ላይ ባሉ ሹፌሮቻችሁ እጅ እና በቴሌብር/ሲቢኢ (CBE) ባንክ ውስጥ የትኛው ክፍያ ከየትኛው ደረሰኝ ጋር እንደገባ በደቂቃዎች ውስጥ ለይተው ማወቅ ይፈልጋሉ? ደይሊ ክሎዝ (Daily Close) የባንክ እና የቴሌብር ገቢዎችን ከዱቤ ደረሰኞች ጋር በሰከንዶች ውስጥ አዛምዶ የትኛው ሱቅ እንዳልከፈለ እና የትኛው ሹፌር ጥሬ ገንዘብ እንዳላስረከበ ወዲያውኑ ያሳውቅዎታል::"*

---

## 3. 15-Minute Wholesale Owner Live Demo Script

**Persona:** Ato Girma, Owner of *Girma & Sons Edible Oil & FMCG Wholesale*, Sebategna, Merkato.  
**Key Pain Point:** 800,000+ ETB in weekly credit sales; 4 Isuzu delivery trucks collecting mix of CBE mobile transfers and cash bags; owner spends 2 hours every evening manually scrolling SMS and paper ledger.

### Minute 00:00 – 02:00: Context & The Morning Brief
1. Open Daily Close on a laptop or rugged tablet at `http://localhost:8000/`.
2. Point to the **Morning Collections Brief**:
   - *"Ato Girma, as soon as you wake up at 7:00 AM, your system prints this plain-text brief or delivers it to your Telegram."*
   - Highlight: Total overdue credit, 3 kiosks with 60+ day balances, and the top 5 retailers to place on credit-stop before today's dispatches.

### Minute 02:00 – 05:00: Step 1 & 2 — Automatic Bank Statement Matching
1. Navigate to **AR Dashboard** (`/`).
2. Demonstrate **Statement Import**:
   - Upload a sample CBE CSV and Telebirr Excel export containing 15 customer transactions with ambiguous narrative names (e.g., `FT2610A7X - TRF FROM KALEAB WORKU`).
3. Click **Auto-Match Payments**:
   - Show how the algorithm resolves Tier A (exact invoice tokens), Tier B (phone / CBE account mapping), and Tier C (normalized Amharic spelling parity).
   - Point out: *"Look at this deposit from Kaleab Worku. He didn't type an invoice number, but because Daily Close remembers his phone number and account, it linked the 45,000 ETB directly to Invoice #INV-2026-089."*

### Minute 05:00 – 08:00: Module 1 — Multi-Warehouse Inventory & GRN Stock In
1. Navigate to **Inventory** (`/inventory`).
2. Show multi-warehouse overview:
   - Kaliti Central Depot vs. Merkato Shop Store.
3. Demonstrate a live **Stock Receipt (GRN)**:
   - 100 cartons of Sunflower Cooking Oil received from Mojo Edible Oil Factory.
   - Point out that unit cost is updated in Santim ($1,050.00\text{ ETB} = 105,000\text{ Santim}$), stock quantity jumps from 50 to 150 on-hand, and low-stock alerts update in real-time.

### Minute 08:00 – 11:00: Module 2 & 3 — Isuzu Delivery Dispatch & 2-Person Cash Vault Handover
1. Navigate to **Delivery Fleet & Custody** (`/delivery`).
2. Walk through Driver Kassahun's route in Isuzu plate `AA-3-B12345`:
   - 4 retail kiosk drop-offs across Piassa and Kazanchis.
   - Show 2 stops paid via CBE transfer; 2 stops paid in physical cash on the road ($35,000\text{ ETB}$).
3. **The Vault Handover Moment:**
   - Driver returns to Kaliti warehouse with cash bag.
   - Demonstrate the **Two-Person Rule**: Driver declares $34,500\text{ ETB}$; warehouse cashier counts $34,500\text{ ETB}$.
   - System instantly flags a **500.00 ETB discrepancy** between the delivery stop ledger and the vault envelope.
   - *"Ato Girma, without Daily Close, this 500 ETB disappears unnoticed. With Daily Close, neither the driver nor the cashier can close the trip without supervisor resolution."*

### Minute 11:00 – 13:00: Module 4 & 5 — Ethiopian Tax & Peachtree / Sage 50 Export
1. Navigate to **General Ledger & Tax** (`/accounting`).
2. Show the **Double-Entry Trial Balance**:
   - Debits equal Credits ($\sum \text{Dr} == \sum \text{Cr}$).
   - P&L shows Gross Profit and Net Income calculated automatically from closed sales and COGS.
3. Click **Export Peachtree / Sage 50 (CSV)**:
   - *"Your external accountant who visits twice a month no longer needs to re-type receipts into Peachtree. With one click, they import every journal entry directly into desktop Sage 50."*
4. Click **MOR VAT Declaration (CSV)**:
   - Show the 15% VAT and 2% Withholding breakdown formatted strictly according to Ethiopian Ministry of Revenues reporting requirements.

### Minute 13:00 – 15:00: The Close & Next Steps
- Ask: *"Ato Girma, if this saves you 2 hours every evening and prevents just one driver cash discrepancy per week, would you be open to running Daily Close in parallel with your paper book for 14 days?"*

---

## 4. 10-Question Merchant Discovery Matrix

During the interview, use this structured questionnaire to uncover operational gaps:

| # | Diagnostic Question | What to Listen For (Red Flags / Opportunities) |
|---|---------------------|------------------------------------------------|
| **1** | How many credit customers do you currently extend goods to, and what is your typical repayment term? | >50 retailers; verbal terms (7-15 days); reliance on memory or notebook. |
| **2** | How do customers pay you (CBE Birr, CBE account transfer, Telebirr, physical cash, cheques)? | Multiple bank accounts (CBE + Dashen + Telebirr) where payments come in without reference numbers. |
| **3** | When a customer transfers 50,000 ETB from their brother's or spouse's bank account, how do you find out whose invoice it belongs to? | Owner checks SMS on personal phone, calls cashier, delays delivery, or loses track. |
| **4** | How do delivery drivers handle cash collections on the road, and what happens when their cash bag is short at night? | Drivers carry large cash sums; shortages are argued verbally; no signed digital audit trail. |
| **5** | Do you store goods in more than one location (e.g. Merkato shop + Kaliti or Kality warehouse)? How do you know stock levels right now? | Phone calls to warehouse guard; surprise stock-outs; stock shrinkage discovered only at annual count. |
| **6** | When suppliers increase factory prices (e.g., cooking oil or sugar), how do you track your purchase debt (AP) and aging? | Overdue supplier debt leads to factory cut-off; payments made without tracking remaining invoice balance. |
| **7** | How does your external accountant get your numbers? | Accountant collects physical paper vouchers at month-end; manually re-enters into desktop Peachtree. |
| **8** | How do you handle Ethiopian Withholding Tax (2%) on purchases $\ge 10,000$ ETB and monthly 15% VAT declarations? | Late filing penalties; withholding receipt slips misplaced; discrepancies during Ministry of Revenues audit. |
| **9** | What happens to your operations when the internet goes down or Ethio Telecom data is slow? | Cloud-only ERPs stop working; workers revert to scrap paper and fail to sync later. |
| **10** | If a cashier or delivery driver leaves tomorrow, how long does it take for a new hire to understand who owes what? | Complete chaos for weeks; unrecorded credit lost permanently. |

---

## 5. Objection-Handling Guide (Ethiopian Merchant Context)

### Objection 1: "Tax & Government Visibility"
- *Merchant:* *"If I put all my sales and bank transfers into this system, will the Ministry of Revenues (ERCA/MOR) have direct access to inspect my records?"*
- *Response:*  
  *"Daily Close is completely private and self-hosted on your own computer or private Ethiopian server. It does not send data to the government or any third party. You control exactly what gets exported. The MOR VAT report is simply a tool for your accountant to calculate your legal 15% VAT and 2% withholding receipts quickly without paying audit penalty fines."*

### Objection 2: "Internet Downtime & Ethio Telecom Reliability"
- *Merchant:* *"Internet in Merkato goes down for hours or days. Cloud systems lock us out when the line drops."*
- *Response:*  
  *"Daily Close runs locally on your office network. You do NOT need active internet to create sales, dispatch drivers, manage warehouse stock, or balance daily cash. When internet is active, you can import bank CSVs or receive Telegram alerts; when it is down, your local system never stops."*

### Objection 3: "Staff Turnover & Cashier Trust"
- *Merchant:* *"My cashiers and warehouse boys change frequently. If a cashier makes a mistake or colludes with a driver, can they change old numbers?"*
- *Response:*  
  *"Daily Close is built on an append-only event ledger. Cashiers cannot delete, edit, or overwrite past transactions or confirmed payments. Only the owner has supervisor authority, and every action leaves a permanent timestamped audit record."*

### Objection 4: "We Already Use Peachtree (Sage 50)"
- *Merchant:* *"My accountant already uses Peachtree on his laptop. Why do I need another software?"*
- *Response:*  
  *"Peachtree is an accounting software for accountants at month-end, not a daily wholesale operations tool. Peachtree cannot match Telebirr SMS, cannot track driver delivery cash bags on the road, and cannot send payment reminders to kiosks. Daily Close manages your daily floor operations and then exports clean, balanced journal entries straight into Peachtree with 1 click."*

---

## 6. Pilot Engagement Terms & Success Metrics

For wholesale merchants entering a 14-day field evaluation:
1. **Pilot Objective:** Reduce end-of-day reconciliation time from 2 hours to under 15 minutes.
2. **Reconciliation Target:** $>90\%$ of electronic statement deposits matched automatically without phone calls.
3. **Cash Custody Target:** Zero unexplained driver cash shortages at vault handover.
4. **Accountant Target:** Month-end journal import into Peachtree completed in under 5 minutes without manual data entry.
