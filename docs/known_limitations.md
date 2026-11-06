# Known Limitations & Honest Disclosures
### Daily Close — Birana Technology (Addis Ababa, Ethiopia)

This document provides a transparent, candid summary of current system limitations, unverified assumptions, and technical trade-offs in Daily Close.

---

### 1. Data Verification & Real-World Inputs
- **No real customer data has been tested:** All validation, benchmarks, and tests have been performed strictly using synthetic data generators and deterministic fixtures.
- **Bank and Telebirr export formats are not verified against real files:** Importers are structured against documented specifications and sample schemas for Commercial Bank of Ethiopia (CBE), Dashen Bank, and Telebirr, but they have not yet been calibrated against live production statement extracts from Ethiopian banking portals.

### 2. User Interface & Runtime Behavior
- **With JavaScript disabled, confirmation dialogs do not appear:** HTMX and browser confirmation dialogs (`hx-confirm` and `window.confirm`) require JavaScript. If JavaScript is disabled, forms still submit and actions execute via standard HTTP POST, but the browser will not prompt the user for confirmation prior to submission.
- **Password reset is disabled in demo mode:** For security and demonstration predictability, staff password resets are disabled on demo tenants (`is_demo=True`).
- **The CSP still allows inline styles:** The Content Security Policy (`style-src 'self' 'unsafe-inline'`) permits inline style attributes to support dynamic CSS variables and layout components without requiring an external build step.
- **The 2,000-payment performance test depends on CPU speed:** The 2,000-payment matching benchmark executes under 15 seconds on modern development workstations, but performance is CPU-bound and may vary on constrained hardware.

### 3. Legal, Regulatory & Architectural Boundaries
- **Legal questions are still open:**
  - *Accounting Licence:* Clarification is required on whether automated reconciliation and ledger balancing constitute regulated accounting services under Ethiopian law.
  - *Telegram Data Residency:* While the core application server runs sovereignly on local infrastructure, notifications and receipt intake via Telegram transmit metadata through Telegram's global infrastructure.
  - *Directive 1142 Article 14:* Directive 1142 Article 14 concerns accreditation guarantees and requires legal counsel review prior to commercial launch.
- **The ERP branch is unreviewed and not part of this build:** The `feat/erp-inventory` branch represents experimental exploration and has not been audited, reviewed, or incorporated into this release.
- **No AI features exist yet; matching uses fixed rules by design:** Daily Close uses 100% deterministic mathematical matching tiers (exact reference, phone/account matching, and phonetic transliteration normalizers). It does not employ Large Language Models (LLMs) or probabilistic AI for financial reconciliation decisions.
