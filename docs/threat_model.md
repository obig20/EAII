# Threat Model: Daily Close (ዕለታዊ ሂሳብ ማጠቃለያ)
**Birana Technology — Addis Ababa, Ethiopia**  
*Document Version:* 1.0.0 | *Classification:* Confidential / Internal Security Review  
*Scope:* Modular Monolith, Multi-Tenant AR Tracking, Statement Importers, Retailer Confirmations, Telegram Quick Entry

---

## 1. System Overview & Trust Boundaries

Daily Close provides Ethiopian wholesalers and distributors with daily accounts receivable (AR) tracking, credit collection management, and month-end accountant reconciliation. It operates strictly within a single Ethiopian-hosted Linux server to satisfy sovereign data residency requirements (Non-Negotiable Rule 7).

```
[ Public Internet / Ethio Telecom / Safaricom ET ]
                    │
                    ▼
┌─────────────────── Trust Boundary 1: Perimeter (TLS / Rate Limiting) ────────────────┐
│   Nginx Reverse Proxy (:443)                                                         │
│   - TLS 1.3 Termination, HSTS, CSP, nosniff, DENY frame-ancestors                   │
│   - Rate Limit: 15 req/min per IP on /confirm-balance/*                              │
└───────────────────────────────────────┬──────────────────────────────────────────────┘
                                        │ (Unix Socket / 127.0.0.1:8000)
┌───────────────────────────────────────▼──────────────────────────────────────────────┐
│   FastAPI Web Application (Uvicorn / Python 3.12)                                    │
│   - Security Headers Middleware                                                      │
│   - 10MB Upload Cap, Allowed Extension Whitelist (.csv, .xlsx)                       │
│   - In-Memory Sliding Window Rate Limiter                                            │
│   - Authentication & Role Binding (Owner, Cashier)                                   │
└───────────────────┬───────────────────────────────────────────┬──────────────────────┘
                    │                                           │
┌───────────────────▼───────────────────┐   ┌───────────────────▼──────────────────────┐
│   Telegram Adapter (aiogram 3)        │   │   Kernel Domain Logic (app/kernel)       │
│   - Cryptographic Invite Codes        │   │   - Integer Santim Validator (Rule 1)    │
│   - Photo Intake -> L4 Evidence Only  │   │   - Evidence Ladder Guards (Rule 2)      │
│   - Role Permission Enforcers         │   │   - Deterministic Matcher (Rule 6)       │
└───────────────────┬───────────────────┘   └───────────────────┬──────────────────────┘
                    │                                           │
┌───────────────────┴───────────────────────────────────────────┴──────────────────────┐
└─────────────────── Trust Boundary 2: Tenant Isolation & Data Persistence ────────────┘
│   PostgreSQL 16 Database Engine                                                      │
│   - App Role: Non-superuser (`app`), zero DDL privileges                             │
│   - Row-Level Security: `FORCE ROW LEVEL SECURITY` on all tenant-scoped tables        │
│   - DB Constraint: `REVOKE UPDATE, DELETE ON events FROM app` (Append-Only Rule 4)   │
│   - Host Filesystem: Nightly OpenSSL AES-256-CBC Encrypted Backups                   │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Threat Actor Personas & Profiles

| Actor Profile | Description & Motivation | Threat Vector & Capabilities |
| :--- | :--- | :--- |
| **Delinquent Retailer** | Buyer seeking to avoid payment or dispute legitimate debt. | Generates fake SMS receipts, submits altered Telebirr screenshots, abuses confirmation links, disputes valid invoices. |
| **Dishonest Cashier** | Employee attempting to conceal stolen cash or pocket collections. | Inputs fake invoices, tries to approve unverified L4 payment screenshots, tampers with sequence numbers. |
| **Rival Wholesaler (Tenant Hopping)** | Another tenant on the shared platform seeking competitor intelligence. | Manipulates HTTP headers (`X-Tenant-ID`), inspects database sequences, attempts cross-tenant SQL injection. |
| **External Internet Attacker** | Opportunistic attacker targeting web endpoints. | Brute forces retailer confirmation tokens, launches zip bombs / XML bombs via XLSX imports, probes for injection. |
| **Telecom / Network Interceptor** | Intermediary sniffing unencrypted traffic or SIM swap fraud. | Attempts MITM on HTTP requests or intercepts unencrypted SMS notifications. |

---

## 3. STRIDE Threat Analysis & Mitigations

### 3.1 Spoofing (Identity & Authenticity)
* **Threat 1: Fabricated SMS / Screenshot Payment Claims:** Retailer or cashier presents a faked Telebirr SMS or Photoshopped CBE deposit slip.
  * *Mitigation (Rule 2 — Evidence Ladder):* Strictly categorizes screenshots and forwarded SMS as **Level L4**. The core domain guard (`assert_confirmable`) mathematically forbids any transition of L4 evidence to `confirmed`. Only L1 (gateway webhook), L2 (bank/Telebirr statement line), or L3 (buyer signed balance confirmation) can set a payment to `confirmed`.
* **Threat 2: Forging Retailer Confirmation Links:** Attacker attempts to guess confirmation tokens to manipulate balances.
  * *Mitigation:* Tokens are generated using CSPRNG (`secrets.token_urlsafe(32)`, 256 bits of entropy). Stored exclusively as SHA-256 hashes (`hashlib.sha256`) and compared using constant-time comparison (`hmac.compare_digest`). Rate-limited to 15 requests/minute.

### 3.2 Tampering (Data Integrity)
* **Threat 3: Retroactive Ledger Modification:** Cashier or attacker modifies past sales or cancels an invoice to hide theft.
  * *Mitigation (Rule 4 — Append-Only Event Log):* All state mutations emit events into the `events` table. PostgreSQL database permissions enforce `REVOKE UPDATE, DELETE ON events FROM app`. Corrections require new compensating events.
* **Threat 4: Skipping Invoices in Sequence:** Cashier hides an off-the-books sale by skipping invoice numbers.
  * *Mitigation (Rule 5 — Invoice-Agnostic Gap Detection):* Continuous sequence gap detection (`detect_sequence_gaps`) scans prefixes and flags sequence interruptions upon registration.

### 3.3 Repudiation (Non-Denial of Actions)
* **Threat 5: Retailer Denies Credit Purchase:** Buyer claims goods were returned or they never owed the balance.
  * *Mitigation:* Retailer balance confirmation records explicit dispute reasons (`ALREADY_PAID`, `WRONG_AMOUNT`, `GOODS_RETURNED`, `UNRECOGNIZED`, `OTHER`) with mandatory explanation text, creating an immutable audit trail in the event log.

### 3.4 Information Disclosure (Confidentiality)
* **Threat 6: Cross-Tenant Data Leakage:** Wholesaler A reads Wholesaler B's customer lists, sales volumes, or debt aging.
  * *Mitigation (Rule 8 — Multi-Tenant Row-Level Security):* Every entity has `tenant_id`. PostgreSQL `FORCE ROW LEVEL SECURITY` isolates queries at the database kernel level based on `app.current_tenant`. Even table owners cannot bypass RLS under the `app` role.
* **Threat 7: PII & Financial Leakage in Server Logs:** Financial amounts or phone numbers exposed in log files.
  * *Mitigation (Rule 9 — Redaction Policy):* Logging amounts or customer PII is forbidden above `DEBUG` level. Production runs strictly at `INFO` level.
* **Threat 8: Cross-Border Data Residency Violations:** Data exported to overseas cloud services (AWS, Azure, analytics SDKs).
  * *Mitigation (Rule 7 — Sovereign Hosting):* Single server running inside Ethiopia. Zero external telemetry, crash reporters, or third-party fonts/analytics.

### 3.5 Denial of Service (Availability)
* **Threat 9: XLSX Zip Bomb / Memory Exhaustion:** Attacker uploads a 1KB zip archive that decompresses into 50GB of XML, crashing the server.
  * *Mitigation:* Standard library XLSX parser verifies total uncompressed size $\le 50\text{MB}$, maximum zip members $\le 1000$, and compression ratio $\le 100:1$ before reading sheet data.
* **Threat 10: Massive File Uploads:** Uploading huge CSV files to consume disk and RAM.
  * *Mitigation:* Hard 10MB streaming file size limit (`MAX_UPLOAD_SIZE = 10 * 1024 * 1024`) with instant HTTP 413 rejection.

### 3.6 Elevation of Privilege (Authorization)
* **Threat 11: Cashier Approving Unverified Cash Payments:** Cashier uses API or Telegram bot to mark an unverified receipt as confirmed.
  * *Mitigation:* Domain permission guards restrict `mark_accepted_unverified` exclusively to the `owner` role. Even if a cashier invokes the endpoint, the application raises HTTP 403 / Domain error.

---

## 4. Evidence Ladder Formal Security Invariant

Let $E(p) \in \{L1, L2, L3, L4\}$ be the evidence level of payment $p$, and $S(p) \in \{\text{unmatched}, \text{proposed}, \text{confirmed}, \text{accepted\_unverified}, \text{rejected}\}$ be its state.

$$\forall p, \quad (S(p) = \text{confirmed}) \implies E(p) \in \{L1, L2, L3\}$$
$$\forall p, \quad (E(p) = L4) \implies S(p) \in \{\text{unmatched}, \text{proposed}, \text{accepted\_unverified}, \text{rejected}\}$$

**Financial Invariant:** For any aging report, morning brief, or accountant export, the effective paid amount $P_{\text{paid}}(inv)$ applied to invoice $inv$ is:
$$P_{\text{paid}}(inv) = \sum \{ m.\text{matched\_santim} \mid m.\text{invoice\_id} = inv.\text{id} \land m.\text{state} = \text{confirmed} \}$$
*Note:* Payments with state $\text{accepted\_unverified}$ contribute exactly $0$ santim to $P_{\text{paid}}(inv)$.

---

## 5. Security Recommendations for Addis Ababa Production Host
1. **Network Firewalls (UFW):** Permit incoming traffic only on TCP ports 22 (SSH with key authentication only), 80 (HTTP redirect), and 443 (HTTPS). Block port 5432 from external interfaces.
2. **PostgreSQL Role Separation:** Ensure the application connects solely using role `app` (never `postgres`). Verify password complexity ($\ge 32$ random characters).
3. **Backup Key Management:** Store the OpenSSL AES-256-CBC backup passphrase in an offline physical vault and secure environment file; never commit it to source control.
