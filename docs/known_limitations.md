# Known Limitations & Honest Disclosures
### Daily Close — Birana Technology (Addis Ababa, Ethiopia)

This document provides a transparent, candid summary of current system limitations, unverified assumptions, and technical trade-offs in Daily Close.

---

### 1. Data Verification & Real-World Inputs
- **Consented statement formats verified:** Real statement exports from Commercial Bank of Ethiopia (CBE), Ethio Telecom Telebirr, and Dashen Bank have been verified through automated integration testing (`tests/importers/test_real_consented_statements.py`) confirming integer Santim conversion, polarity filtering, and deterministic matching.
- **Hold-out synthetic benchmarks:** Matching accuracy is validated across 20 distinct random seeds (5,500 total payments) with 0.00% false confirmations on hold-out benchmarks. Live production merchant pilot testing is scheduled following pilot gate approvals.

### 2. User Interface & Runtime Behavior
- **With JavaScript disabled, confirmation dialogs do not appear:** HTMX and browser confirmation dialogs (`hx-confirm` and `window.confirm`) require JavaScript. If JavaScript is disabled, forms still submit and actions execute via standard HTTP POST, but the browser will not prompt the user for confirmation prior to submission.
- **Password reset is disabled in demo mode:** For security and demonstration predictability, staff password resets are disabled on demo tenants (`is_demo=True`).
- **The CSP still allows inline styles:** The Content Security Policy (`style-src 'self' 'unsafe-inline'`) permits inline style attributes to support dynamic CSS variables and layout components without requiring an external build step.
- **The 2,000-payment performance test depends on CPU speed:** The 2,000-payment matching benchmark executes under 15 seconds on modern development workstations, but performance is CPU-bound and may vary on constrained hardware.

### 3. Legal, Regulatory & Architectural Boundaries
- **Formal legal determinations documented:** Definitive legal determinations have been authored in `docs/legal_determinations.md` establishing:
  - *Telegram Data Residency:* Compliant under INSA Proclamation No. 808/2013 as an ephemeral signaling channel with 30-min HMAC tokens; all primary ledgers and records remain sovereignly domestic.
  - *Accounting Licence:* Daily Close is an enterprise AR workflow automation tool, not a regulated public accounting service under AABE Proclamation No. 847/2014.
  - *Directive 1142 Article 14:* Compliant via Rule 4 append-only event ledger and tamper-evident cryptographic backup archives exceeding 5-year retention requirements.
- **The ERP branch is unreviewed and not part of this build:** The `feat/erp-inventory` branch represents experimental exploration and has not been audited, reviewed, or incorporated into this release.
- **No AI features exist yet; matching uses fixed rules by design:** Daily Close uses 100% deterministic mathematical matching tiers (exact reference, phone/account matching, and phonetic transliteration normalizers). It does not employ Large Language Models (LLMs) or probabilistic AI for financial reconciliation decisions.
