# Pilot Readiness & Operational Status
**Daily Close (ዕለታዊ ሂሳብ ማጠቃለያ) — Birana Technology, Addis Ababa, Ethiopia**  
*Document Version:* 1.1.0 | *Milestone:* v0.3 Stabilization & Diagnostic Audit  
*Status:* **Ready for demos, diagnostic audits, and structured interviews; pilot gates NOT COMPLETE.**

---

## 1. Readiness Executive Summary

Daily Close is an accounts receivable and daily settlement reconciliation platform designed specifically for Ethiopian fast-moving consumer goods (FMCG) and food wholesalers (Merkato, Piassa, regional trading corridors).

> [!IMPORTANT]
> **Operational Status: All Pilot Readiness Gates are NOT DONE.**  
> While all core application functionality, multi-tenant Row-Level Security (RLS), diagnostic health checks, Payer Memory, collections friction measurement, and the NullVerifier design interface pass 100% of the automated test suite under the unprivileged `app` role, **no real-world customer pilot has commenced**. Real-world deployment remains strictly gated until all formal operational gates in Section 4 are executed and verified with live human and legal stakeholders.

---

## 2. Capabilities Verification Summary (v0.1 – v0.3)

### v0.1 & v0.2: Core Reconciliation & Safety
- **Deterministic Evidence Ladder (Rule 2):** Payments with Level L4 evidence (receipt photos, forwarded SMS) are strictly forbidden from confirmation via the immutable core domain guard (`assert_confirmable`).
- **Owner Match Reversals & Concurrency:** Confirmed allocations can only be reversed by authenticated owners with valid business reason codes, logged into an append-only ledger (`events`). Row locking (`with_for_update()`) and `payment_lines.is_previously_reversed` prevent double-reversals and race conditions.
- **Wholesale Demo Mode & Health Check CLI:** Deterministic synthetic seed data and non-invasive diagnostic health-check CLI (`python -m app.cli health-check`) providing bilingual (Amharic / English) AR aging analysis.

### v0.3: Pilot Hygiene, Payer Memory & Collections Friction
- **Hygiene & Authentication Hardening:** Strict CSRF and owner-only authorization across all administrative POST endpoints. Public retailer token view events are strictly idempotent (written once per token without IP or PII logging).
- **Payer Memory (`payer_aliases`):** Stores third-party payer mappings (spouse, sibling, purchasing agent) explicitly confirmed by business owners. Payments from recognized payers generate proposals (`MatchTier.TIER_B`/`TIER_C` with state `proposed`) and **never auto-confirm** or modify `matcher.py` auto-confirm invariants.
- **Real Problem Measurement:** Collections Health Check and Pilot Metrics compute five empirical friction aggregates:
  1. Share of statement credits containing open invoice numbers in transfer notes.
  2. Share matching retailer phone or account.
  3. Share matched through confirmed payer memory.
  4. Share of payments with no usable remittance identifier.
  5. Median owner daily review minutes.
  *(All metrics report explicit sample sizes $N$ and safely display "not available" when $N=0$.)*
- **Remittance Instruction Prompt:** Proactively instructs retailers via reminders and balance confirmation links: *"Please write the invoice number in your transfer note"* (all Amharic copy marked `<!-- needs native review -->`).
- **Receipt Verification (Design Only):** Comprehensive architecture specification (`docs/design/receipt_verification.md`) and fail-closed `NullVerifier` interface (`app/kernel/receipt_verifier.py`). No network code, zero socket calls, and zero modifications to `EvidenceLevel` enum.

---

## 3. Adversarial Security & Invariant Audit

All 14 adversarial security findings (SEC-VULN-01 to SEC-VULN-14) remain actively enforced and tested under forced PostgreSQL Row-Level Security (`NOBYPASSRLS`):

| Check / Domain | Security Enforcement | Verification Suite |
| :--- | :--- | :--- |
| **Multi-Tenancy** | PostgreSQL `FORCE ROW LEVEL SECURITY` on all tables with `tenant_id`. | `tests/kernel/test_rls.py` (22+ isolation tests) |
| **Auto-Confirm Safety** | Auto-confirmation strictly limited to Tier A exact invoice token + equal amount + confirmable evidence (`L1`, `L2`, `L3`). Zero changes to `matcher.py`. | `tests/kernel/test_matcher.py`, `tests/adversarial/` |
| **Anti-Tampering** | Append-only event log (`REVOKE UPDATE, DELETE ON events FROM app`). Reversals require compensating events. | `tests/kernel/test_events.py` |
| **XSS & Injection** | Local CSS/SVG only; zero inline scripts; zero `on*` attributes; zero `|safe` or `innerHTML` in templates. | `tests/security/test_security_audit.py` |
| **Network Decoupling** | Zero outbound network calls, zero AI/LLM dependencies during reconciliation. | `tests/kernel/test_receipt_verifier_design.py` |

---

## 4. Formal Pilot Readiness Gates (All NOT DONE)

Before any live pilot deployment with consenting wholesale merchants in Addis Ababa can be authorized, each of the following prerequisite operational gates must be completed. **Currently, every single gate is NOT DONE**:

| Gate # | Operational Gate | Strict Completion Criteria | Status | Unverified Blockers |
| :---: | :--- | :--- | :---: | :--- |
| **1** | **Hold-Out Accuracy Gate** | Blind hold-out dataset from real merchant transactions demonstrating exactly zero false auto-confirmations (0.00%) under production matching rules. | **NOT DONE** | No real merchant dataset has been collected or evaluated. |
| **2** | **Consented Real Statements Gate** | Live ingestion and reconciliation testing of real bank (CBE, Awash, Dashen) and Telebirr statements from consenting Ethiopian merchants. | **NOT DONE** | Importers calibrated only against documented schemas and synthetic files; real bank portal variations unverified. |
| **3** | **HTTPS & Transport Security Gate** | Production TLS/HTTPS deployment with trusted CA certificates, strict HSTS headers, secure cookie flags, and verified reverse-proxy isolation. | **NOT DONE** | Staging and local testing executed over local HTTP/dev servers. |
| **4** | **Sovereign Ethiopian Hosting Gate** | Physical server provisioning and deployment on domestic infrastructure within Ethiopia satisfying Rule 7 data residency constraints. | **NOT DONE** | Production domestic host not yet provisioned or audited. |
| **5** | **Second-Machine Restore Drill Gate** | Disaster recovery drill restoring encrypted `age` database backups on an independent physical machine from bare metal. | **NOT DONE** | Rehearsal performed only in local directory clones; physical cross-machine drill unexecuted. |
| **6** | **Regulatory & Licensing Compliance Gate** | Formal legal review and signed determinations regarding National Bank of Ethiopia (NBE) payment processing rules, Accounting and Auditing Board of Ethiopia (AABE) requirements, and Ministry of Revenue Directive 1142 Article 14 accreditation guarantees. | **NOT DONE** | Legal counsel review pending; regulatory status uncertified. |
| **7** | **Merchant Consent & Privacy Notice Gate** | Executed bilateral merchant data consent agreements, retailer privacy disclosures, and data subject notification agreements compliant with the Ethiopian Personal Data Protection legal framework. | **NOT DONE** | No real merchants or retailers have executed consent agreements. |

---

## 5. Summary Release Gate Command

To verify complete codebase integrity and automated test execution at any time:
```bash
python -m app.cli release-gate
```
*Note: Passing automated tests proves synthetic invariant compliance; it does NOT substitute for the operational pilot gates above.*
