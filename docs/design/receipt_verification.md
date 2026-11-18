# Receipt Verification — Architecture & Integration Design

> **STATUS: DESIGN ONLY — NOT BUILT**  
> **DISCLAIMER**: This document represents architectural design and preliminary analysis only. No network integrations, external API clients, or production verifiers have been implemented. No new enum variants have been introduced to `app/kernel/enums.py`. All samples and scenarios discussed herein are strictly synthetic.

---

## 1. Executive Summary & Purpose

In Ethiopian wholesale commerce (e.g., Mercato, Piazza, regional hubs), retailers frequently send digital payment confirmations via SMS, Telebirr push notifications, or mobile banking screenshots before the bank statement line appears in the wholesaler's account.

This design document specifies the proposed architecture for a **Receipt Verification Service** to authenticate electronic payment receipts directly against issuing financial institutions (e.g., Telebirr, CBE Birr, Awash Bank, Dashen Bank) prior to statement generation, while strictly preserving Daily Close's core safety invariants:

1. **Zero Auto-Confirmation**: Receipt verification alone can **never** auto-confirm a credit sale or balance. It produces candidate proposals with status `PROPOSED` for owner review only.
2. **Offline-Safe & Decoupled**: The core kernel never makes synchronous outbound network calls during matching or ledger operations.
3. **Fail-Closed to Pending**: If a verification provider is degraded or unreachable, status transitions to `PENDING` (never assumed failed or auto-accepted).

---

## 2. Placement in the Evidence Ladder

The current Daily Close Evidence Ladder defines four formal tiers:
* **L1**: Real-time Bank Direct Feed (future certified API)
* **L2**: Bank Statement Line (CSV / MT940 / official export) — *Minimum threshold for auto-confirmation*
* **L3**: Customer Self-Service Balance Confirmation (signed cryptographic token)
* **L4**: Cashier / Retailer Photo or Unverified Slip (strictly informational, never deductible)

### Proposed Architectural Tier (Design Level Only): L2.5 / Verified Digital Receipt

```
+-------------------------------------------------------------+
| L1: Real-time Bank Direct Feed                              |
+-------------------------------------------------------------+
| L2: Official Bank Statement Line (CSV / MT940)             |  <-- Auto-confirm boundary
+ - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - +
| [DESIGN ONLY] Verified Digital Receipt                      |  <-- Strictly PROPOSED
| (Cryptographically verified via issuer portal or query API) |
+-------------------------------------------------------------+
| L3: Customer Self-Service Balance Confirmation Token       |
+-------------------------------------------------------------+
| L4: Unverified Photo / Screenshot / Manual Entry           |
+-------------------------------------------------------------+
```

### Safety Invariants:
* **Enum Preservation**: In accordance with the project safety rules, **no new evidence level is added to `EvidenceLevel` in `app/kernel/enums.py`**. The system continues to use existing enum values (`L1`, `L2`, `L3`, `L4`).
* **Evidence Hierarchy**: A verified digital receipt ranks **strictly below** an official statement line (L2) and **above** an unverified photo (L4).
* **Proposal Only**: Matches produced via verified receipts are assigned `MatchTier.TIER_B` or `TIER_C` with state `proposed`. Under no circumstances may a verified receipt bypass the owner's manual confirmation.

---

## 3. Required Verification Checks

Before a digital receipt can be recognized as a valid proposal candidate, the verifier must execute four mandatory atomic checks:

### 3.1 Receiver Account Authentication
* The destination account or merchant till number in the receipt payload must match the verified merchant credentials of the wholesaler tenant.
* Cross-merchant receipts (payments made to third parties) must be rejected immediately.

### 3.2 Exact Amount Validation (Integer Santim)
* The receipt amount must match the expected transaction amount to the exact integer santim.
* Floating-point calculations are prohibited; arithmetic must use integer `amount_santim`.
* Partial settlements must be flagged explicitly as partials and cannot match single full invoices.

### 3.3 Provider Finality / Transaction Status
* The transaction state reported by the issuer must represent irreversible finality (e.g., `COMPLETED`, `SUCCESSFUL`, `SETTLED`).
* Pending, initiated, reversed, or cancelled receipts must fail verification.

### 3.4 Single-Use Replay Prevention (Nonce / Receipt Reference)
* Every receipt reference (e.g., Telebirr transaction number, CBE transaction ID) must be globally unique per tenant.
* A cryptographic hash of the issuer receipt ID + date + amount must be stored with a unique database constraint (`uq_receipt_nonce_tenant`).
* Re-presenting an already verified receipt must be flagged as a duplicate replay attack.

---

## 4. Failure Modes & Graceful Degradation

| Failure Mode | Root Cause | System Behavior | Evidence / Audit Log |
|---|---|---|---|
| **Provider Unreachable** | Timeout, DNS failure, 5xx gateway error | Mark verification **`PENDING`**; queue for exponential backoff retry. | `receipt.verification_deferred` event; no change to proposals. |
| **Issuer Rate Limit** | HTTP 429 Too Many Requests | Pause external verification queue; throttle worker; keep state **`PENDING`**. | `receipt.rate_limited` log with backoff interval. |
| **Mismatched Receiver** | Customer paid wrong merchant account | Mark verification **`REJECTED`** with clear reason. | `receipt.rejected` event with reason `wrong_receiver`. |
| **Replay / Duplicate** | Same transaction code submitted twice | Mark verification **`DUPLICATE_REPLAY`**; flag to owner. | `receipt.duplicate_detected` event. |
| **Ambiguous Match** | Amount matches multiple open invoices | Verification status **`VERIFIED`** but matching engine assigns **`AMBIGUOUS`** state. | Pushed to owner review queue for manual allocation. |

**Guiding Rule**: Any provider failure or network uncertainty defaults to **`PENDING`**. The system never speculates or assumes settlement.

---

## 5. Open Legal, Regulatory, and Privacy Questions

Implementation of automated receipt verification in Ethiopia faces significant legal and regulatory complexities that must be resolved prior to production engineering:

### 5.1 Personal Data Protection & Consent
* **FDRE Data Protection Legal Framework**: Processing payer personal details (e.g., payer name, phone number, bank account number) extracted from bank APIs or notifications constitutes processing of personal data.
* **Third-Party Consent**: When an employee, sibling, or third party pays on behalf of a retailer, their personal data enters the wholesaler's system without prior direct consent. Does the wholesaler have a "legitimate interest" exemption under upcoming Ethiopian data protection legislation?

### 5.2 Banking Regulations & Direct Bank APIs
* **Terms of Service of Providers**: Telebirr, CBE Birr, and private banks (Awash, Dashen, etc.) generally prohibit automated screen scraping or unauthorized API querying without formal corporate merchant aggregator agreements.
* **Financial Data Intermediation**: Does operating an automated receipt query service require licensing as a Payment System Operator (PSO) or Payment Instrument Issuer (PII) under National Bank of Ethiopia (NBE) Directive ONPS/01/2020?

### 5.3 Data Residency & Infrastructure
* Under Ethiopian cybersecurity and financial directives, financial transaction records and customer identifiers must remain resident within Ethiopian jurisdiction. Cloud hosting or proxy routing through overseas data centers is legally restricted.

### 5.4 Third-Party Aggregators & Audit Liability
* If a third-party gateway (e.g., Chapa, SantimPay, Kifiya) is engaged to verify bank transactions:
  * Who bears liability in the event of an erroneous verification resulting in goods dispatch against a bounced or reversed payment?
  * Wholesalers require contractual indemnification from aggregators against fraudulent receipt falsification.

---

## 6. NullVerifier Interface Specification

To establish a cleanly decoupled interface without introducing runtime network code, the kernel provides a `ReceiptVerifier` protocol and a default `NullVerifier` implementation.

* **Interface**: Defined in `app/kernel/receipt_verifier.py`.
* **Behavior**: Always returns status `PENDING`, `verified=False`, reason `"Receipt verification disabled by default (Design only)"`.
* **Zero Outbound Calls**: Guarantees zero network sockets, zero dependencies, and zero side effects.
