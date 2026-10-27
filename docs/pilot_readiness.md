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

## 3. Adversarial Security Verification (SEC-VULN-01 to SEC-VULN-13)

All 13 security findings from the independent adversarial review have been remediated and verified under PostgreSQL role `app` (`NOBYPASSRLS`):

| Vulnerability ID | Description | Remediation Status | Verification Test |
| :--- | :--- | :--- | :--- |
| **SEC-VULN-01** | `pg_dump` failure under RLS role | **REMEDIATED** | `test_sec_vuln_01_pg_dump_under_app_role_fails_due_to_rls` |
| **SEC-VULN-02** | Double credit via concurrent approval | **REMEDIATED** | `test_attack_double_counting_payment_line`, `test_attack_parallel_approvals_race_condition` |
| **SEC-VULN-03** | CSV formula injection (CWE-1236) | **REMEDIATED** | `test_csv_formula_injection_in_sales_export` |
| **SEC-VULN-04** | Telegram HTML injection / bot crash | **REMEDIATED** | `test_telegram_html_injection_in_handle_review` |
| **SEC-VULN-05** | Proxy header IP spoofing / owner lockout | **REMEDIATED** | `test_x_forwarded_for_trusted_proxies` |
| **SEC-VULN-06** | Argon2id login timing oracle | **REMEDIATED** | `test_dummy_argon2id_timing_normalization` |
| **SEC-VULN-07** | Client IP address logging in event ledger | **REMEDIATED** | `test_ip_address_not_in_events_payload` |
| **SEC-VULN-08** | Insecure production startup configuration | **REMEDIATED** | `test_production_startup_validation` |
| **SEC-VULN-09** | Pre-auth login lookup blocked by RLS | **REMEDIATED** | `test_pre_auth_login_lookup_under_app_role` |
| **SEC-VULN-10** | Duplicate statement ingestion | **REMEDIATED** | `test_statement_import_duplicate_row_becomes_possible_duplicate_review_item` |
| **SEC-VULN-11** | Matching engine false confirmations | **REMEDIATED** | `test_synthetic_benchmark_10_seeds`, `test_independent_generator_10_seeds` (0 false confirmations across all seeds) |
| **SEC-VULN-12** | Foreign key crash on unknown login account | **REMEDIATED** | `test_unknown_user_login_does_not_crash_fk` |
| **SEC-VULN-13** | CSV upload crash on null bytes (`\x00`) | **REMEDIATED** | `test_null_bytes_in_csv_upload` |

---

## 4. Pending Gates Before Live Pilot Deployment

Live pilot deployment with consenting wholesale merchants requires completion of the following prerequisite gates:

1. **Merchant Consent & Data Agreement Gate:**
   - Formal bilateral data processing agreement signed with pilot merchant owners in Addis Ababa.
   - Merchant-specific retailer master list audit and initial invoice opening balance reconciliation.
2. **Cryptographic Key Management Gate:**
   - Air-gapped offline generation of production `age` master private key (`AGE-SECRET-KEY-1...`).
   - Secure provisioning of the public recipient key (`age1...`) to `/opt/dailyclose/backup_pubkey.txt` on the sovereign host.
   - Physical cold-storage deposition of the recovery key in a bank safety deposit box in Addis Ababa.
3. **SMS Gateway & Telecommunications Gate:**
   - Production SMPP / SMS gateway account credentials provisioned with Ethio Telecom / Safaricom Ethiopia.
   - Dedicated shortcode registration and callback webhook secrets configuration.
4. **Physical Host Infrastructure Gate:**
   - Linux host physical hardening, firewall lockdown (ports 80/443 only), and automated nightly backup cron setup.
   - End-to-end disaster recovery drill from cold backup on the production server.

---

## 5. Summary Release Gate Command

To verify complete system integrity at any time, run:
```bash
python -m app.cli release-gate
```
This executes the full automated test suite under the `app` role, validates Alembic migration head alignment, verifies Evidence Ladder immutability, and confirms demo and diagnostic readiness.
