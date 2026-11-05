# Merchant Field Discovery Interview Runbook
**Daily Close (ዕለታዊ ሂሳብ ማጠቃለያ) — Birana Technology, Addis Ababa, Ethiopia**  
*Document Version:* 0.2.0-interview-ready | *Date:* October 28, 2026  
*Status:* **Ready for demos and interviews; pilot gates not complete.**

---

## 1. Purpose & Scope

This runbook guides customer discovery interviews and software demonstrations with wholesale trade business owners (*Ato / Weizero*) and cashiers in Addis Ababa commercial districts (e.g., Merkato, Sebategna, Piassa).

**Objectives:**
1. Discover how wholesale merchants currently track credit sales and reconcile customer bank/Telebirr transfers against outstanding balances.
2. Demonstrate the verified Daily Close Accounts Receivable (AR) reconciliation workflows (sales capture, bank statement matching, aging analysis, and morning collections brief).
3. Gather feedback on operational edge cases, local terminology, and dispute-handling practices.

> [!WARNING]
> **Regulatory and Tax Disclaimer:**
> Daily Close is strictly an internal accounts receivable tracking and bank statement reconciliation tool. It does **NOT** provide tax calculation, tax filing, tax compliance verification, or official fiscal receipt generation. Do **NOT** tell merchants their records or exports are "tax compliant", "tax-ready", or approved by the Ministry of Revenues (MOR) or the Accounting and Auditing Board of Ethiopia (AABE). All legal and regulatory determinations remain `[TO VERIFY: Regulatory Counsel]`.

---

## 2. Standard 15-Minute Demonstration Protocol

This demonstration covers **only** verified features present on the `master` branch. It does not demo inventory, route delivery dispatches, tax calculations, or general ledger accounting.

### Minute 00:00 – 03:00: Context & The Morning Brief
1. Navigate to Web Dashboard at `http://localhost:8000/`.
2. Present the **Morning Collections Brief**:
   - Plain-text summary ranking overdue retailer accounts by age and amount.
   - Show how it lists overdue debtors (Current, 1–30, 31–60, 61–90, 90+ days) with contact phone numbers to organize morning collection phone calls.

### Minute 03:00 – 07:00: Credit-Sale Capture & Sequence Validation
1. Demonstrate **Sale Recording**:
   - Record a single credit sale (`INV-101`, Retailer, Amount in ETB, Due Date).
   - Demonstrate sequence gap detection (missing invoice numbers in a sequence are flagged as warnings for internal control).
2. Demonstrate **Telegram Quick Entry** (`app/channels/telegram/`):
   - Type quick message: `INV381 | Abebe Shop | 120000 | due 30d`.
   - Show immediate reflection on the dashboard.

### Minute 07:00 – 11:00: Bank Statement Ingestion & 3-Tier Matcher
1. Navigate to **Statement Import**:
   - Upload sample statement (CBE CSV, Telebirr XLSX, or Dashen CSV).
2. Run **Payment Matcher**:
   - **Tier A:** Exact invoice number found in reference with identical amount $\implies$ Auto-confirmed (with L1/L2/L3 evidence).
   - **Tier B:** Customer phone/account number matched with exact amount $\implies$ Routed to review queue as proposed.
   - **Tier C:** Phonetic/transliterated Amharic name similarity $\implies$ Routed to review queue as proposed.
   - **Review Queue:** Show owner-only approval, differing amount confirmation, and unallocated credit remainder tracking.

### Minute 11:00 – 15:00: AR Aging Analysis & Export
1. Display the **5-Bucket AR Aging Report**:
   - Show how confirmed payments reduce remaining debtor balances.
   - Show that unconfirmed/proposed items (`accepted_unverified`) **never** reduce debtor balances (Rule 2 Evidence Ladder).
2. Download **Accountant CSV Export**:
   - Section 1: Sales Ledger.
   - Section 2: Verified Confirmed Payments.
   - Section 3: Reversed Matches Audit Log.

---

## 3. Merchant Discovery Interview Guide

| # | Question for Merchant Owner / Cashier | What to Note & Validate |
|---|---------------------------------------|-------------------------|
| **1** | How many credit customers do you extend goods to, and what are typical credit repayment terms? | `[TO VERIFY: Merchant field baseline]` — informal vs formal terms, paper notebooks vs Excel. |
| **2** | Which payment methods do your buyers use most frequently (CBE account transfer, Telebirr, CBE Birr, Dashen, cash, cheques)? | Distribution across payment channels and frequency of reference-less transfers. |
| **3** | When a customer transfers funds from a third-party account (e.g. spouse, brother) without an invoice reference, how do you identify who paid? | Manual lookup methods, phone calls, SMS screenshots forwarded by buyers. |
| **4** | How do you handle customer payment disputes (e.g., buyer claims already paid, returned damaged goods)? | Evidence required to settle disputes; role of signed delivery notes or paper receipts. |
| **5** | How do you deliver month-end sales and payment summaries to your external bookkeeper or accountant? | Paper voucher handover vs Excel spreadsheets; file format preferences. |
| **6** | What happens during local internet or telecommunication outages? | Offline resilience requirements in commercial wholesale markets. |

---

## 4. Objection-Handling Reference (Verified Scope Only)

### Objection 1: "Does this software report my data to the Ministry of Revenues?"
- **Response:**  
  *"No. Daily Close is completely private and runs locally on your own computer or private server. It does not connect to the government, tax authorities, or any third party. It is strictly an internal accounts receivable record to help you track who owes you money."*

### Objection 2: "Is this system tax-compliant or approved by tax authorities?"
- **Response:**  
  *"No. Daily Close is not an electronic fiscal device, does not calculate taxes, and does not submit tax declarations. It is strictly for internal credit sales tracking and bank payment reconciliation. Your accountant must review all records before filing tax declarations."*

### Objection 3: "What if our internet connection drops in the market?"
- **Response:**  
  *"Daily Close runs on your local network. You can record credit sales, view aging reports, and review reconciliations without an active internet connection. Active internet is only needed when downloading electronic bank statements or sending Telegram messages."*

### Objection 4: "Can staff change past numbers or delete transactions?"
- **Response:**  
  *"No. Daily Close uses an append-only event ledger. Cashiers cannot edit or delete past recorded sales or payments. Approvals and reversals are restricted to the business owner, and every change records an immutable audit log entry."*

---

## 5. Items Marked TO VERIFY Prior to Pilot

The following operational, technical, and regulatory items cannot be confirmed from repository code and require formal verification before commercial deployment:
1. `[TO VERIFY: Line 25]` Specific wholesale customer payment term distributions (7 days vs 15 days vs 30 days).
2. `[TO VERIFY: Line 28]` Average daily transaction volumes for FMCG vs building materials wholesalers.
3. `[TO VERIFY: Line 45]` Legality of customer balance confirmation SMS under Ethiopian telecommunications regulations.
4. `[TO VERIFY: Line 52]` AABE bookkeeping software certification requirements under Directive 1142.
5. `[TO VERIFY: Line 68]` Exact bank statement narrative formats for Awash Bank and Bank of Abyssinia.
