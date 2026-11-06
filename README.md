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
7. **Sovereign Data Residency:** The web application and database run entirely on a server operated by Birana Technology. Telegram is used as an optional notification and intake channel and is a third-party service outside our control; all primary financial records are stored only on our server.
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

### Option B: Local Python Environment Setup (Step-by-Step for Fresh Machine)

Follow these exact steps in order on a fresh host machine:

#### Step 1: Create and Activate Virtual Environment
```bash
# Linux / macOS
python3 -m venv .venv
source .venv/bin/activate

# Windows PowerShell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

#### Step 2: Install Repository Dependencies
Choose the appropriate requirements file:
- `requirements/dev.txt`: Recommended for local development, demo runs, and full test suite execution (includes `pytest`, `httpx`, `coverage`, `factory-boy`).
- `requirements/base.txt`: Core application dependencies only (`fastapi`, `uvicorn`, `sqlalchemy`, `alembic`, `psycopg`, `aiogram`, `argon2-cffi`, `defusedxml`).
- `requirements/prod.txt`: Production server deployment dependencies.

```bash
pip install --upgrade pip
pip install -r requirements/dev.txt
```

#### Step 3: Configure Environment Variables (`.env`)
Copy the template configuration file:
```bash
cp .env.example .env
```
Open `.env` and configure the following parameters (replace placeholder values with your own secure keys; **never commit secrets to version control**):
- `DATABASE_URL`: Application connection string for the unprivileged `app` role:
  `postgresql+psycopg://app:your_app_password@localhost:5432/dailyclose_demo`
- `DATABASE_MIGRATION_URL`: Migration connection string for the PostgreSQL superuser:
  `postgresql+psycopg://postgres:your_superuser_password@localhost:5432/dailyclose_demo`
- `SECRET_KEY`: 64-character random hex string for signing session tokens and approval tokens. Generate using:
  `python -c "import secrets; print(secrets.token_hex(32))"`
- `APP_ENV`: Set to `development` for local demo and feature testing (or `production` for strict production safeguards).
- `LOG_LEVEL`: Set to `INFO` (Rule 9: never set to `DEBUG` in production to prevent PII leakage).

#### Step 4: Provision PostgreSQL Databases & Roles
Connect to PostgreSQL as `postgres` superuser (using `psql` or pgAdmin):

```sql
-- 1. Create the unprivileged application role
CREATE ROLE app WITH LOGIN PASSWORD 'your_app_password';

-- 2. Create the demo database and the test database
CREATE DATABASE dailyclose_demo OWNER postgres;
CREATE DATABASE dailyclose_test OWNER postgres;

-- 3. Grant connection privileges to the application role
GRANT CONNECT ON DATABASE dailyclose_demo TO app;
GRANT CONNECT ON DATABASE dailyclose_test TO app;
```

> [!NOTE]
> When Alembic runs under the superuser connection (`DATABASE_MIGRATION_URL`), migration `0001_initial.py` automatically grants `SELECT, INSERT, UPDATE, DELETE` on all public tables to `app`, `USAGE, SELECT` on sequences, enables and forces Row-Level Security (RLS), and revokes `UPDATE, DELETE` on the `events` table from `app` to enforce append-only audit immutability.

#### Step 5: Run Database Migrations
Apply the complete schema migrations up to head (`0006_stage2_demo_mode`):
```bash
alembic upgrade head
```

#### Step 6: Seed the Demo Wholesaler Tenant
Seed a realistic Addis Ababa wholesale tenant with 12 retailers, 30 invoices, and 9 statement entries:
```bash
python -m app.cli create-demo
```

#### Step 7: Launch the Application Server
```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```
> [!IMPORTANT]
> **Host Binding:** Always use `--host 127.0.0.1` for local work. Use `--host 0.0.0.0` only on a trusted network. The demo login is public.

#### Step 8: Log In to the Web Dashboard
Open your browser to `http://127.0.0.1:8000/login` and log in:
- **Identifier:** `demo@dailyclose.et`
- **Password:** `DemoDailyClose2026!`

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

## 🎭 Wholesale Demo Environment & Database (`dailyclose_demo`)

Daily Close includes a pre-packaged, realistic demo mode representing a Merkato/Sebategna wholesale distributor (12 retailers, 30 invoices, 9 statement payment lines).

> [!WARNING]
> **Do not run pytest while the demo database is configured as the test database.** Pytest executes automated table teardown and recreation cycles against `dailyclose_test`. Ensure your `.env` or settings explicitly separate `dailyclose_demo` (for running the demo) from `dailyclose_test` (for running test suites).

### 🚀 Start the demo in 3 commands

Run these 3 commands from the repository root to launch the Addis Ababa wholesale demo:

```bash
# 1. Apply database migrations
alembic upgrade head

# 2. Seed realistic Addis Ababa wholesale demo tenant
python -m app.cli create-demo

# 3. Launch the local web server
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

> [!IMPORTANT]
> **Host Binding:** Always use `--host 127.0.0.1` for local work. Use `--host 0.0.0.0` only on a trusted network. The demo login is public.

Open your browser to **`http://127.0.0.1:8000/login`** and sign in:
- **Identifier:** `demo@dailyclose.et`
- **Password:** `DemoDailyClose2026!`

Follow the complete 2-minute walkthrough guide in [`docs/demo_script.md`](docs/demo_script.md).

---

### Database Provisioning & Isolation

Daily Close uses separated databases:
- **Demo Database:** `dailyclose_demo`
- **Test Database:** `dailyclose_test`

```bash
# Create dedicated demo database in PostgreSQL
createdb -h localhost -p 5432 -U postgres dailyclose_demo

# Apply schema migrations to dailyclose_demo
DATABASE_URL=postgresql://app:app_password@localhost:5432/dailyclose_demo alembic upgrade head
```

### Reset and Re-seed Commands

To reset or re-seed the wholesale demo:

1. **Reset an existing demo tenant back to baseline:**
   ```bash
   python -m app.cli reset-demo --tenant-id <tenant_id>
   ```
   *(Alternatively, owners can click the "Reset Demo Data" button in the web dashboard notification banner).*

2. **Re-seed a fresh demo tenant from scratch:**
   ```bash
   python -m app.cli create-demo
   ```

This populates:
- **Tenant:** Addis Wholesale Demo / አዲስ ጅምላ ማሳያ (`is_demo=True`)
- **Owner Account:** Ato Demo Owner (`demo@dailyclose.et` / `+251911999901`) with password `DemoDailyClose2026!`
- **Cashier Account:** W/ro Demo Cashier (`demo.cashier@dailyclose.et` / `+251922999902`) with password `DemoCashier123!`
- **12 Retailers:** Merkato, Piassa, Bole, Mexico, etc.
- **30 Invoices:** Spread across all 5 AR aging buckets (Current, 1-30, 31-60, 61-90, 90+ days overdue). 27 open/partial + 3 settled, with ~4,686,000 ETB in total open credit.
- **9 Bank/Telebirr Lines:** Demonstrating Tier A auto-confirmation, Tier B review queue, Tier C fuzzy match, partial payment with warning, overpayment with unallocated credit, and L4 unverified receipts.

### Production Safety Guard
> [!IMPORTANT]
> **Production Guard:** `create-demo`, `reset-demo`, and hardcoded demo passwords refuse to run whenever `APP_ENV=production` (`RuntimeError` or `PermissionError`). Demo data can never be seeded into or wiped from a production tenant.

---

## 🖥️ Command-Line Interface (CLI)

Daily Close provides an administrative CLI via `python -m app.cli`. Below is the complete syntax and flag options extracted directly from `--help`:

### 1. `bootstrap-owner`
Bootstrap initial tenant and owner account.
```text
usage: cli.py bootstrap-owner [-h] --tenant TENANT --identifier IDENTIFIER
                              --name NAME [--password PASSWORD]

options:
  -h, --help            show this help message and exit
  --tenant TENANT       Tenant / Business name
  --identifier IDENTIFIER
                        Owner email or phone (+251...)
  --name NAME           Owner display name
  --password PASSWORD   Owner password (min 10 chars; prompts if omitted)
```

### 2. `create-telegram-invite`
Generate Telegram registration invite code.
```text
usage: cli.py create-telegram-invite [-h] --tenant-id TENANT_ID
                                     --role {owner,cashier} --name NAME
                                     [--days DAYS]

options:
  -h, --help            show this help message and exit
  --tenant-id TENANT_ID
                        Tenant UUID
  --role {owner,cashier}
                        User role
  --name NAME           Staff display name
  --days DAYS           Expiry in days (default: 7)
```

### 3. `prune-auth-failures`
Prune auth_failures older than N days.
```text
usage: cli.py prune-auth-failures [-h] [--days DAYS]

options:
  -h, --help   show this help message and exit
  --days DAYS  Prune failures older than N days (default: 7)
```

### 4. `unlock-user`
Unlock a locked user account and reset failed login attempts.
```text
usage: cli.py unlock-user [-h] --identifier IDENTIFIER

options:
  -h, --help            show this help message and exit
  --identifier IDENTIFIER
                        User email or phone (+251...)
```

### 5. `create-demo`
Create a realistic Addis Ababa wholesale demo tenant.
```text
usage: cli.py create-demo [-h] [--tenant TENANT] [--identifier IDENTIFIER]
                          [--name NAME] [--password PASSWORD]

options:
  -h, --help            show this help message and exit
  --tenant TENANT       Demo tenant name
  --identifier IDENTIFIER
                        Owner login identifier
  --name NAME           Owner display name
  --password PASSWORD   Owner password (min 10 chars)
```

### 6. `reset-demo`
Safely reset a demo tenant back to baseline data.
```text
usage: cli.py reset-demo [-h] --tenant-id TENANT_ID

options:
  -h, --help            show this help message and exit
  --tenant-id TENANT_ID
                        Demo Tenant UUID
```

### 7. `health-check`
Run Collections Health Check diagnostic.
```text
usage: cli.py health-check [-h] --invoices INVOICES
                           --statements STATEMENTS [STATEMENTS ...]
                           [--out OUT] [--lang {en,am}] [--map MAP] [--keep]

options:
  -h, --help            show this help message and exit
  --invoices INVOICES   Path to credit sales / invoices CSV
  --statements STATEMENTS [STATEMENTS ...]
                        Path to one or more bank/mobile money statement files
                        (CSV/XLSX)
  --out OUT             Output path for self-contained HTML report (default:
                        report.html)
  --lang {en,am}        Report language: 'en' (English) or 'am' (Amharic)
                        (default: en)
  --map MAP             Optional JSON file for column mappings
  --keep                Keep the temporary tenant data instead of rolling
                        back/deleting
```

### 8. `release-gate`
Run full release gate verification.
```text
usage: cli.py release-gate [-h]

options:
  -h, --help  show this help message and exit
```

---

## 🚢 Single-Host Linux Deployment (Addis Ababa)

A complete deployment package is included in the `deploy/` directory:

1. **Nginx Reverse Proxy (`deploy/nginx.conf`):**
   - TLS 1.3 termination, rate limiting on confirmation links, HSTS, CSP, and security headers.
2. **Systemd Services (`deploy/*.service`):**
   - `dailyclose-web.service` and `dailyclose-bot.service` with process sandboxing (`ProtectSystem=full`, `PrivateTmp=true`).
3. **Nightly Encrypted Backups (`deploy/backup_nightly.sh`):**
   - Automatically dumps PostgreSQL data at 02:00 EAT daily.
   - Encrypts dump using **`age` asymmetric public-key encryption** (`age1...` recipients). No symmetric passphrase is stored on the server; only the offline private key can decrypt.
   - Generates SHA-256 integrity checksums.
   - Enforces a 30-day retention pruning policy.
   - Backups are stored locally on the server; no external cloud transmission (Rule 7).

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
