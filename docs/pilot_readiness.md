# Pilot Readiness & Operational Status
**Daily Close (ዕለታዊ ሂሳብ ማጠቃለያ) — Birana Technology, Addis Ababa, Ethiopia**  
*Document Version:* 1.0.0 | *Date:* October 27, 2026  
*Status:* **Ready for demos and interviews; pilot gates not yet complete.**

---

## 1. Readiness Executive Summary

The Daily Close accounts receivable tracking and daily settlement reconciliation platform has successfully reached the **Demo and Interview Readiness** milestone (Git Tag `v0.2-interview-ready`). 

> [!IMPORTANT]
> **Operational Status:**  
> **Ready for demos and interviews; pilot gates not yet complete.**  
> While all core application functionality, adversarial safety guards, demo mode data generators, and diagnostic health-check CLI tools are fully verified and passing 100% of the automated test suite under the unprivileged `app` role, live production deployment to pilot wholesalers remains subject to the completion of formal pilot operational gates described in Section 4.

---

## 2. Completed Capabilities (Stages 1–3)

### Stage 1: Review Queue Safety & Match Reversal
- **Deterministic Evidence Ladder (Rule 2):** Payments with Level L4 evidence (receipt photos, unconfirmed SMS) are strictly forbidden from confirmation via the immutable core domain guard (`assert_confirmable`).
- **Owner Match Reversals:** Confirmed allocations can only be reversed by authenticated owners, requiring valid business reason codes (`wrong_retailer`, `duplicate_payment`, `fraudulent_or_disputed_claim`, `incorrect_allocation_amount`, `cancelled_sale`).
- **Audit Logging & Free-Text Privacy:** Every reversal records an immutable event (`payment.match_reversed`) in the tenant ledger. Free-text operational notes are segregated into the dedicated, deletable `reversal_notes` table with Row-Level Security (RLS).
- **Concurrency & Double-Reversal Guards:** Row locking (`with_for_update()`) and the database column `payment_lines.is_previously_reversed` prevent double-reversals and race conditions between approvals and reversals.
- **HMAC Action Tokens:** Review actions use 30-minute scoped tokens signed with `SECRET_KEY` and verified with `hmac.compare_digest`. Telegram buttons strictly observe the 64-byte payload limit using compact 8-character signatures.

### Stage 2: Wholesale Demo Mode
- **Realistic Addis Ababa Business Data:** Built-in seed generator produces an Ethiopian food and FMCG wholesaler with 12 distinct retailers (e.g. Merkato, Piassa, Bole, Megenagna), 30 open/partial invoices (1,230,000 ETB total credit), and 9 statement lines across CBE, Telebirr, and Dashen Bank.
- **Safe Reset CLI & Web API:** One-click reset restores the tenant to the pristine baseline while preserving audit logs and tenant settings. Non-demo production tenants are strictly protected against accidental reset (`ck_tenants_is_demo`).

### Stage 3: Collections Health Check CLI
- **Non-Invasive Diagnostic (`python -m app.cli health-check`):** Ingests wholesaler credit sales CSV and bank/Telebirr statements, executes the deterministic matching engine, computes AR aging analysis, and outputs a self-contained single-file HTML report.
- **Bilingual Interface:** Instant client-side English/Amharic toggle (e.g. Merkato market terms: ዕለታዊ ሂሳብ ማጠቃለያ, ያልተከፈለ ቀሪ, በጊዜ የተከፋፈለ ዕዳ).
- **Zero Third-Party CDN Dependencies:** All fonts, scripts, and charts are fully embedded inline for reliable offline field usage across Addis Ababa.

---

## 3. Adversarial Security Verification (SEC-VULN-01 to SEC-VULN-14)

All original 13 security findings from the independent adversarial review, plus the new finding SEC-VULN-14, have been remediated and verified under PostgreSQL role `app` (`NOBYPASSRLS`):

| Vulnerability ID | Category / Description | Severity | Remediation Summary | Verification Test |
| :--- | :--- | :---: | :--- | :--- |
| **SEC-VULN-01** | Database / RLS Backup (`pg_dump` failure under RLS) | High | Automated backups execute under dedicated `backup_role` with `BYPASSRLS`. Script enforces `age` asymmetric encryption. | `test_sec_vuln_01_pg_dump_under_app_role_fails_due_to_rls`, `test_task_21_backup_tamper_detection`, `test_backup_encryption_age_public_key_mode` |
| **SEC-VULN-02** | Concurrency / Double Credit on Match Approval | Critical | Approval uses `SELECT ... FOR UPDATE` with lock ordering. Rejects already confirmed/settled items with HTTP 409. | `test_attack_double_counting_payment_line`, `test_attack_parallel_approvals_race_condition` |
| **SEC-VULN-03** | CSV Formula Injection (CWE-1236) | High | Sanitized CSV cells prepending `'` to trigger characters (`=`, `@`, `\t`, `\r`, non-numeric `+`/`-`). Exempts valid phone numbers. | `test_csv_formula_injection_in_sales_export`, `test_csv_phone_numbers_survive_and_formulas_neutralized` |
| **SEC-VULN-04** | Stored XSS / Telegram HTML Injection & Bot Crash | High | Dynamic fields (`retailer.name`, `raw_reference`, `invoice_number`) escaped with `html.escape()` prior to rendering with `parse_mode=HTML`. | `test_telegram_html_injection_in_handle_review` |
| **SEC-VULN-05** | Auth / Network Spoofing & Owner Lockout DoS | High | Trust `X-Forwarded-For` / `X-Real-IP` ONLY when immediate peer is in `trusted_proxies`. Replaced blanket lockout with progressive per-(account, IP) delays. | `test_task_12_x_forwarded_for_spoofing_and_owner_lockout`, `test_cli_unlock_user` |
| **SEC-VULN-06** | Argon2id Login Timing Oracle | Low | Normalized timing for non-existent vs existing users via dummy Argon2id hash computation. | `test_task_11_account_enumeration_via_fk_crash`, `test_dummy_argon2id_timing_normalization` |
| **SEC-VULN-07** | Privacy / Append-Only Log (Plaintext IP in events) | Medium | IP addresses purged from immutable `events` table payloads. Ephemeral retention only in `rate_limit_buckets` and `sessions`. | `test_task_13_ip_address_stored_in_immutable_events` |
| **SEC-VULN-08** | Production Startup Hardening | High | Pydantic `model_validator` in `Settings`: production mode strictly rejects default/short `SECRET_KEY`, `debug=True`, insecure cookies, and `DEBUG` log level. | `test_task_15_production_settings_validation_failures`, `test_task_15_valid_production_config_starts_cleanly` |
| **SEC-VULN-09** | Multi-Tenancy / RLS Blocks Login under App Role | Critical | Strict RLS on `users`/`sessions` preserved; narrow `SECURITY DEFINER` function `lookup_user_for_auth` owned by `auth_lookup_role` used for pre-auth lookup. | `test_task_10_app_role_rls_blocks_unauthenticated_user_lookup`, `test_task_3_cross_tenant_idor_rejected` |
| **SEC-VULN-10** | Statement Import Deduplication & Idempotency | High | Computes SHA-256 hash of statement files, prevents re-ingestion of duplicate batches, flags duplicate payment rows. | `test_statement_import_duplicate_row_becomes_possible_duplicate_review_item`, `test_batch_hash_deduplication` |
| **SEC-VULN-11** | Matching Engine False Confirmations (Tier B amount coincidence) | Critical | Auto-confirmation restricted strictly to Tier A exact invoice token matches with equal amount and confirmable evidence (`L1`, `L2`, `L3`). Tier B and Tier C demoted to review queue as `proposed`. | `test_synthetic_benchmark_10_seeds`, `test_independent_generator_10_seeds` (0 false confirmations across 10 seeds) |
| **SEC-VULN-12** | Foreign Key Crash on Unknown Login Identifier | High | Auth failures for unknown identifiers no longer insert dummy tenant UUIDs into `events`. Recorded in dedicated `auth_failures` table with HMAC-SHA256 identifier and no FK. | `test_task_11_account_enumeration_via_fk_crash` |
| **SEC-VULN-13** | File Handling / CSV Null Byte DataError & XML Injection | High | Uploaded files sanitized: null bytes (`\x00`) rejected; UTF-8 validated. Migrated XLSX parser to `defusedxml` with DTD disabled defending against billion laughs and XXE. | `test_null_bytes_in_csv_upload`, `test_billion_laughs_xml_in_xlsx_blocked`, `test_utf16_encoded_xxe_in_xlsx_blocked` |
| **SEC-VULN-14** *(NEW)* | Single-Open-Invoice Ambiguity Bypass | High | Removed single-open-invoice ambiguity bypass. If a payment reference cites an invoice token that does not exist among open invoices, it routes to review queue as `proposed` and never auto-confirms an unrelated invoice. | `test_attack_nonexistent_invoice_token_does_not_auto_confirm_unrelated` |

---

## 4. Pending Gates Before Live Pilot Deployment

Live pilot deployment with consenting wholesale merchants is subject to completion of the following prerequisite gates. Each gate is currently marked **NOT DONE**:

| Pilot Gate Item | Requirement & Description | Current Status |
| :--- | :--- | :---: |
| **1. Hold-Out Accuracy Gate** | Blind hold-out dataset test demonstrating exactly zero false confirmations (0.00%) under production matching rules. | **NOT DONE** |
| **2. Consented Real Statements Gate** | Live ingestion and reconciliation testing of two or three real bank/Telebirr statements from consenting Ethiopian merchants. | **NOT DONE** |
| **3. HTTPS & Transport Security Gate** | TLS/HTTPS configuration with trusted certificate authority, HSTS headers, and secure cookie domain binding. | **NOT DONE** |
| **4. Sovereign Ethiopian Hosting Gate** | Physical server provisioning and deployment on domestic infrastructure within Ethiopia (Rule 7 Data Residency). | **NOT DONE** |
| **5. Second-Machine Restore Drill Gate** | Disaster recovery drill restoring encrypted `age` backups on an independent physical machine from bare metal. | **NOT DONE** |
| **6. Regulatory & Licensing Compliance Gate** | Formal legal review and signed determinations on Telegram data residency, Accounting and Auditing Board of Ethiopia (AABE) licensing requirements, and Ministry of Revenue Directive 1142 Article 14 record retention. | **NOT DONE** |
| **7. Merchant Consent & Privacy Notice Gate** | Executed bilateral merchant data consent agreements, retailer privacy disclosures, and data subject notification forms. | **NOT DONE** |

---

## 5. Summary Release Gate Command

To verify complete system integrity at any time, run:
```bash
python -m app.cli release-gate
```
This executes the full automated test suite under the unprivileged `app` role (including slow benchmarks and the 2,000-payment performance test), validates Alembic migration head alignment, verifies Evidence Ladder immutability, and confirms demo and diagnostic readiness.
