# Daily Close (ዕለታዊ ሂሳብ ማጠቃለያ)
### Birana Technology — Addis Ababa, Ethiopia

[![Python 3.12](https://img.shields.io/badge/Python-3.12-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-green.svg)](https://fastapi.tiangolo.com/)
[![PostgreSQL 16](https://img.shields.io/badge/PostgreSQL-16-blue.svg)](https://www.postgresql.org/)
[![License: Proprietary](https://img.shields.io/badge/License-Proprietary-red.svg)]()

**Daily Close** helps Ethiopian wholesalers and distributors who sell goods on credit answer three critical business questions every single day:
1. **Who owes them money today, and by how many days is each invoice overdue?**
2. **Who has actually paid through their bank accounts or Telebirr?**
3. **What exact reconciliation export does their partner accountant need at month-end?**

---

## 🏛️ Architecture & Principles

Daily Close is built as a **Modular Monolith** designed to run strictly on a single server hosted in Ethiopia.

```
daily-close/
├── app/
│   ├── kernel/             # Pure Domain Core (Entities, Money, Event Log, RLS, Guards)
│   ├── importers/          # Bank & Telebirr Statement Ingestion (CSV & OpenXML XLSX)
│   ├── matching/           # 3-Tier Deterministic Matcher & Transliteration Normalizer
│   ├── reports/            # AR Aging Buckets, Morning Brief, Full & Accountant CSV Exports
│   └── channels/
│       ├── web/            # FastAPI Web Dashboard, HTMX Partials, Retailer Confirmation
│       └── telegram/       # aiogram 3 Bot Adapter (Bilingual English / Amharic)
├── deploy/                 # Production Nginx, Systemd Units & Encrypted Backup Scripts
├── docs/                   # One-Page Threat Model & Security Specifications
└── tests/                  # Pytest Suite (Kernel, RLS, Importers, Matcher, Security, Web, Bot)
```

> [!IMPORTANT]
> **Domain Isolation:** `app/kernel/` contains pure domain entities and business rules. It has zero external dependencies and **NEVER imports FastAPI or aiogram**.

---

## 🔒 The 11 Non-Negotiable Rules

1. **Integer Santim Only:** All monetary amounts are stored and calculated as integer `santim` (`BIGINT`, 1 ETB = 100 santim). Floats are strictly prohibited.
2. **Evidence Ladder for Payments:**
   - **L1:** Signature-verified licensed-gateway webhook.
   - **L2:** Line from an imported bank or Telebirr statement.
   - **L3:** Buyer confirmation through a signed, expiring link.
   - **L4:** Screenshot, forwarded SMS, or manual note.  
   *Guarantee:* Only **L1, L2, or L3** can set a payment to `confirmed`. L4 evidence can only create a `proposed` match. An owner may mark an L4 payment `accepted_unverified`, a separate state that is **NEVER counted as paid** in any balance, aging total, morning brief, or accountant export.
3. **Plurality & Ambiguity Guard:** If two or more open invoices are equally plausible matches for a payment line, it is never auto-confirmed; it is automatically routed to the Owner Review Queue.
4. **Append-Only Event Log:** Every state change emits an event into `events`. Database permissions enforce `REVOKE UPDATE, DELETE ON events FROM app`. Corrections require new compensating events.
5. **Invoice-Agnostic:** The system records externally issued invoices (number, retailer, amount, issue date, due date). It never generates invoices, tax documents, or QR codes. Sequence gaps are automatically detected and flagged.
6. **Zero LLM / AI Decisions:** All payment matching, confirmation, and financial operations use deterministic mathematical rules.
7. **Sovereign Data Residency:** Runs entirely on a single Ethiopian-hosted server. Zero external analytics SDKs, telemetry, or third-party cloud dependencies.
8. **Multi-Tenant Row-Level Security:** Every table has `tenant_id`, strictly isolated via PostgreSQL `FORCE ROW LEVEL SECURITY`. One tenant cannot read or write another tenant's records.
9. **Log Redaction:** Customer PII and financial figures are never logged above `DEBUG` level. Production runs strictly at `INFO` level.
10. **Comprehensive Test Suite:** Every feature ships with automated tests. 100% test pass rate required.
11. **Non-Destructive Deployments:** No commands alter uncommitted data without explicit audit controls.

---

## 🚀 The 4 Core Features

### 1. Credit-Sale Capture & Sequence Validation
- **Web Form & CSV Bulk Upload:** Fast entry with duplicate invoice rejection per tenant.
- **Sequence Gap Detection:** Scans invoice prefixes (e.g. `INV-101`, `INV-102`, `INV-105`) and flags missing numbers (`INV-103`, `INV-104`).
- **Telegram Quick Entry:** On-the-go entry for owners and cashiers:
  ```text
  INV381 | Abebe Shop | 120000 | due 30d
  ```

### 2. Statement Ingestion & 3-Tier Deterministic Matcher
- Supports **Commercial Bank of Ethiopia (CBE CSV)**, **Telebirr (OpenXML XLSX)**, and **Dashen Bank (CSV)** via configurable column mappings.
- **Matching Tiers:**
  - **Tier A (Exact):** Invoice number identified in reference AND equal amounts $\implies$ Auto-confirms (if L1/L2/L3).
  - **Tier B (Counterparty):** Phone or account number match + exact amount + configurable date window.
  - **Tier C (Fuzzy Name):** Ethiopian transliterated name similarity (English/Amharic phonetic normalizer) + amount.
- **Review Queue:** Partial payments, split payments, and equal-amount collisions route safely to owner review.

### 3. Aging Report, Morning Brief & Accountant Exports
- **AR Aging Buckets:** 5 standardized buckets (`Current`, `1-30 days`, `31-60 days`, `61-90 days`, `90+ days overdue`). Confirmed payments strictly reduce balances.
- **Morning Collections Brief:** Ranked plain-text priority list for morning collection calls:
  ```text
  === DAILY CLOSE: MORNING COLLECTIONS BRIEF ===
  1. Abebe Shop: 120,000.00 ETB (45 days overdue) | Phone: +251911223344
  2. Piassa Electronics: 45,000.00 ETB (12 days overdue) | Phone: +251911887766
  ```
- **Partner Accountant Export:** Clean CSV breakdown of sales ledger, verified confirmed payments, and an unverified audit queue.

### 4. Retailer Balance Confirmation & Telegram Bot
- **Retailer Confirmation Portal:** Generates a signed, 7-day expiring link (`/confirm-balance/{raw_token}`).
  - **Confirm Balance:** Allows the retailer to confirm balance or provide payment details (creates **L3 Evidence**).
  - **Dispute Balance:** Requires a reason code (`ALREADY_PAID`, `WRONG_AMOUNT`, `GOODS_RETURNED`, `UNRECOGNIZED`, `OTHER`) and explanation, transitioning invoices to `disputed`.
  - Protected by in-memory sliding-window rate limiting (15 req/min per IP).
- **Telegram Bot (`aiogram 3`):**
  - Cryptographic invite-code registration (`/start DC-XXXXXX`).
  - Role-based permissions (`owner` vs `cashier`).
  - Bilingual localization (**English** & **Amharic - አማርኛ**).
  - Receipt photo intake: strictly creates **L4 evidence** in `proposed` state.
  - Quick commands: `/brief`, `/aging`, `/review`.

---

## 🛠️ Local Development Setup

### Prerequisites
- Python 3.12+
- PostgreSQL 16
- Docker & Docker Compose (optional)

### Option A: Using Docker Compose (Recommended)
```bash
# 1. Clone repository
git clone https://github.com/birana-tech/daily-close.git
cd daily-close

# 2. Configure environment
cp .env.example .env

# 3. Start PostgreSQL, run migrations, and launch web app
docker compose up -d

# 4. Access the web dashboard at http://localhost:8000
```

### Option B: Local Python Virtual Environment
```bash
# 1. Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\Activate.ps1

# 2. Install dependencies
pip install -r requirements/dev.txt

# 3. Configure environment
cp .env.example .env

# 4. Run database migrations (connect as superuser)
alembic upgrade head

# 5. Launch FastAPI application
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

---

## 🧪 Testing & Accuracy Benchmarks

Daily Close ships with a 189-test verification suite covering domain rules, RLS tenant isolation, injection defenses, file upload security, and synthetic accuracy targets:

```bash
# Run all tests
pytest

# Run security audit suite specifically
pytest tests/security/test_security_audit.py -v

# Run synthetic data matching benchmark
pytest tests/matching/test_synthetic_accuracy.py -v
```

### Synthetic Accuracy Benchmark Results
Simulating 30 retailers, 300 invoices, 400 payment lines across 10 noise patterns (missing references, Amharic-transliterated name variations, partial payments, same-amount collisions, date delays):
- **False Confirmations:** **0.00%** (Target: EXACTLY ZERO)
- **Auto-Match Recall:** **91.23%** (Target: $\ge 90\%$)
- **Auto-Match Precision:** **100.00%**
- **Review Queue Share:** **8.77%**

---

## 🚢 Single-Host Linux Deployment (Addis Ababa)

A complete deployment package is included in the `deploy/` directory:

1. **Nginx Reverse Proxy (`deploy/nginx.conf`):**
   - TLS 1.3 termination, rate limiting on confirmation links, HSTS, CSP, and security headers.
2. **Systemd Services (`deploy/*.service`):**
   - `dailyclose-web.service` and `dailyclose-bot.service` with process sandboxing (`ProtectSystem=full`, `PrivateTmp=true`).
3. **Nightly Encrypted Backups (`deploy/backup_nightly.sh`):**
   - Automatically dumps PostgreSQL data at 02:00 UTC daily.
   - Encrypts dump using **OpenSSL AES-256-CBC** with PBKDF2 (100,000 iterations).
   - Generates SHA-256 integrity checksums.
   - Enforces a 30-day retention pruning policy.
   - Zero external cloud transmission (Rule 7).

```bash
# Setup nightly backup cron on server:
crontab -e
# Add line:
0 2 * * * /opt/dailyclose/deploy/backup_nightly.sh >> /var/log/dailyclose/backup.log 2>&1

# Verify backup integrity at any time:
/opt/dailyclose/deploy/verify_backup.sh
```

---

## 📄 License & Confidentiality
Copyright © 2026 Birana Technology PLC, Addis Ababa, Ethiopia.  
Proprietary & Confidential. All rights reserved.
