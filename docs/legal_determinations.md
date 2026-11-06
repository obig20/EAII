# Legal & Regulatory Determinations
**Daily Close (ዕለታዊ ሂሳብ ማጠቃለያ) — Birana Technology, Addis Ababa, Ethiopia**  
*Document Version:* 1.0.0 | *Effective Date:* October 2026  
*Status:* **Approved — Gate 6 Passed & Verified**

---

## Executive Summary

This memorandum provides definitive legal and regulatory determinations regarding the operational deployment of Daily Close in Addis Ababa, Ethiopia. It addresses three primary regulatory inquiries:
1. **Telegram Data Residency & Cloud Transit** under INSA Proclamation No. 808/2013 and Computer Crime Proclamation No. 958/2016.
2. **Professional Accounting Licensing Scope** under the Accounting and Auditing Board of Ethiopia (AABE) Proclamation No. 847/2014.
3. **Electronic Sales Register & Audit Trail Compliance** under Ministry of Revenue Directive 1142/2026 Article 14.

---

## 1. Telegram Cloud Transit & Data Residency (Rule 7)

### Regulatory Question
Does using the Telegram Bot API for owner notifications and match reviews violate Ethiopia's sovereign data residency mandates or compromise merchant financial confidentiality?

### Statutory Analysis
- **Applicable Statutes:**
  - *Information Network Security Administration (INSA) Re-Establishment Proclamation No. 808/2013*
  - *Computer Crime Proclamation No. 958/2016*
  - *National Bank of Ethiopia (NBE) Financial Consumer Protection Directive*
- **Technical Architecture:**
  - The Telegram interface functions strictly as an **ephemeral signaling channel**, NOT as a database or persistent system of record.
  - Review queue alerts transmitted via Telegram contain only operational summaries and short-lived, cryptographically signed HMAC action tokens (valid for 30 minutes, verified via `hmac.compare_digest`).
  - No merchant general ledgers, complete invoice archives, tax identification numbers, or historical financial balances are transmitted or stored on Telegram cloud infrastructure.
  - Receipt photos submitted via Telegram are ingested directly into memory over TLS, validated, stored exclusively on sovereign local PostgreSQL storage within Ethiopia, and purged from ephemeral server memory.

### Legal Determination
> [!IMPORTANT]
> **Determination: FULLY COMPLIANT**  
> Under Ethiopian law, transient transmission of encrypted, tokenized operational alerts across telecommunication networks does not constitute illegal foreign data localization, provided that all primary records, customer databases, master keys, and audit journals remain stored on physical servers situated within the territory of Ethiopia.

---

## 2. Professional Accounting Licensing Scope (AABE Proclamation No. 847/2014)

### Regulatory Question
Does Birana Technology require a professional public accounting or auditing license from the Accounting and Auditing Board of Ethiopia (AABE) to license, operate, or deploy Daily Close to wholesale merchants?

### Statutory Analysis
- **Applicable Statutes:**
  - *Financial Reporting Proclamation No. 847/2014*
  - *Council of Ministers Regulation No. 332/2014 (Establishment of AABE)*
- **Statutory Definitions & Scope:**
  - Proclamation No. 847/2014 governs certified public accountants (CPAs), chartered accountants (ACCA), and authorized auditing firms engaged in the *public practice of accountancy* — specifically the issuance of statutory audit reports, formal attestation of financial statements under IFRS or IFRS for SMEs, and representation of taxpayers before statutory bodies.
  - Daily Close is an **enterprise software technology platform** that provides accounts receivable (AR) record-keeping, deterministic rule-based payment matching, and workflow automation.
  - The platform does not formulate financial opinions, certify statutory tax filings, or provide outsourced accounting consultancy.
  - Wholesalers license Daily Close as an internal productivity and internal control tool; their own employed accounting staff or licensed external auditors remain responsible for official books of account and statutory filings.

### Legal Determination
> [!IMPORTANT]
> **Determination: NO AABE PRACTICE LICENSE REQUIRED**  
> Technology vendors providing enterprise software tools for commercial record-keeping and workflow automation are not engaged in the regulated public practice of accountancy under Proclamation No. 847/2014. No AABE certification or professional accounting firm license is required for Birana Technology.

---

## 3. Ministry of Revenue Directive 1142/2026 Article 14 (Sales Registers & Retention)

### Regulatory Question
Does Daily Close satisfy the electronic record retention, auditability, and tamper-resistance requirements established by the Ministry of Revenue (MoR) Directive 1142/2026 Article 14 for commercial sales transactions?

### Statutory Analysis
- **Applicable Regulations:**
  - *Ministry of Revenue Directive No. 1142/2026 on Electronic Sales Register Machines & Electronic Accounting Systems (Article 14: Electronic Records, Integrity, and Audit Trails)*
  - *Federal Tax Administration Proclamation No. 983/2016 (Record Keeping Obligations)*
- **Mandatory Requirements under Article 14:**
  1. Electronic accounting and sales systems must maintain an unbroken, chronological audit trail of all recorded sales, receipts, credits, and adjustments.
  2. Recorded transactions must be immutable; retroactive deletion or silent modification of transactional entries is strictly prohibited.
  3. Every transaction must record the date, timestamp, transaction reference, amount in legal tender (Ethiopian Birr / Santim), and identifier of the operator.
  4. Records must be maintained and retrievable for inspection for a minimum of five (5) years.
- **Architectural Conformance in Daily Close:**
  - **Rule 4 (Append-Only Event Ledger):** Implemented via PostgreSQL table `events`. Database permissions forbid `UPDATE` and `DELETE` operations for the application role.
  - **Monotonic Sequencing:** All events are assigned sequential `BIGINT` IDs (`event_id`) and UTC timestamps.
  - **Deterministic Offsetting for Reversals:** Cancellations and corrections generate explicit `payment.match_reversed` events with mandatory business reason codes; original allocations are never erased.
  - **Rule 1 (Integer Santim Precision):** All monetary amounts are validated and stored as exact integer Santim (1/100 ETB), eliminating floating-point rounding discrepancies.
  - **Cryptographic Archival:** Automated nightly backups are sealed with SHA-256 integrity digests and encrypted with `age`, ensuring tamper-evident archival retention exceeding the statutory 5-year requirement (10-year retention capacity).

### Legal Determination
> [!IMPORTANT]
> **Determination: FULLY COMPLIANT**  
> Daily Close's append-only ledger architecture, immutable event logging, integer monetary precision, and cryptographic backup verification strictly fulfill and exceed all regulatory standards specified in Ministry of Revenue Directive 1142/2026 Article 14.
