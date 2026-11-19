# Known Limitations & Honest Disclosures
### Daily Close (ዕለታዊ ሂሳብ ማጠቃለያ) — Birana Technology (Addis Ababa, Ethiopia)
*Version:* 0.3.0 | *Last Updated:* November 2026

This document provides a transparent, candid inventory of current system limitations, unverified assumptions, and architectural trade-offs in Daily Close v0.3.

---

### 1. Data Verification & Real-World Inputs
- **All Data is Strictly Synthetic:** All automated tests, benchmarks, and performance metrics are executed strictly against synthetic fixtures and procedural generators. No real customer names, phone numbers, or live banking lines have ever been processed.
- **Bank and Mobile Money Formats are Unverified in Production:** The statement importers for Commercial Bank of Ethiopia (CBE), Dashen Bank, Awash Bank, and Telebirr are built to documented layout specifications. However, live bank portal extracts frequently introduce unannounced whitespace variations, merged headers, localized date strings (e.g. Ethiopian calendar dates), and inconsistent encoding that remain unverified against live files.
- **Amharic Script Transliteration on Real Handsets:** The phonetic normalizer for Amharic and Fidel characters has been verified through unit tests but has not undergone UX review by native Amharic-speaking wholesale operators on real physical Android mobile keyboards (e.g. Gboard, Fyn). All Amharic template strings are flagged with `<!-- needs native review -->`.

---

### 2. Matching Engine & Identification Trade-offs
- **Zero LLM / AI Decisions by Design:** Matching is 100% deterministic (exact token, counterparty normalization, Levenshtein distance). It does not guess or interpolate intention.
- **Payer Memory Requires Manual Owner Curation:** The `payer_aliases` table enables remembering third-party payers (spouse, sibling, purchasing agent), but aliases can **never be created automatically**. An owner must explicitly confirm an alias. If an owner does not curate aliases, third-party payments remain in the review queue.
- **Payer Memory Never Auto-Confirms:** In accordance with the immutable core domain rules, recognized payer aliases only produce `MatchTier.TIER_B` or `TIER_C` candidates in `proposed` state. The business owner must manually approve every allocation.
- **Single-Open-Invoice Ambiguity:** When an unreferenced payment matches an open invoice by amount coincidence, it is routed to the review queue as `proposed`. The system never assumes settlement.

---

### 3. Architecture & Hardware Dependencies
- **Receipt Verification is Design Only (Not Built):** The receipt verification architecture described in `docs/design/receipt_verification.md` is strictly a design specification. The runtime implementation (`app/kernel/receipt_verifier.py`) provides only a `NullVerifier` that returns `pending`. No network clients, socket handlers, or provider APIs exist in the codebase.
- **JavaScript-Disabled Limitations:** All HTML pages and forms are fully functional with JavaScript disabled via standard HTTP POST navigation. However, interactive client-side confirmations (`hx-confirm`, browser dialogs) are bypassed without JavaScript; submissions process immediately upon tapping submit.
- **Single-Server Monolith Performance:** The 2,000-payment matching benchmark executes under 15 seconds on modern development workstations, but matching throughput is strictly CPU-bound. In constrained field environments with older dual-core servers, batch imports may require asynchronous background task workers.
- **Review Queue Daily Minutes Computation:** The metric estimating daily owner review time relies on operational event density (`review.opened`, `review.decision_made`). Until longitudinal pilot usage is observed with real store owners, the metric will frequently report `not available`.

---

### 4. Legal, Regulatory & Sovereign Boundaries
- **All Pilot Readiness Gates are NOT DONE:** No commercial pilot agreement has been signed, and no real wholesaler has granted data processing consent.
- **Accounting & Tax Licensing:** Formal determination from the Accounting and Auditing Board of Ethiopia (AABE) regarding software accreditation remains open.
- **Ministry of Revenue Directive 1142 Article 14:** Legal counsel has not yet formally reviewed whether the software requires formal accreditation guarantees as a fiscal calculation intermediary.
- **National Bank of Ethiopia (NBE) Payment Framework:** Direct receipt verification against Telebirr or CBE APIs requires formal corporate merchant aggregator agreements or Payment System Operator (PSO) licensing under NBE Directive ONPS/01/2020.
- **Telegram Metadata Residency:** While the core database and web application run sovereignly in Ethiopia, Telegram bot communications pass through Telegram's international data infrastructure.
