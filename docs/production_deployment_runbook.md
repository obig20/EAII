# Production Deployment Runbook & Operating Procedures
**Daily Close (ዕለታዊ ሂሳብ ማጠቃለያ) — Birana Technology, Addis Ababa, Ethiopia**  
*Document Version:* 1.0.0 | *Target Environment:* Single Host Linux Server (Ubuntu 22.04 LTS / Debian 12)  
*Status:* **Production Ready**

---

## 1. System Architecture & Operating Principles

Daily Close is engineered as a **Modular Monolith** designed to run strictly on a single sovereign Linux server in Ethiopia.

```
 Internet (Browser / Telegram / Retailers)
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│ Nginx Reverse Proxy (Port 80/443)                           │
│ - TLS 1.2/1.3 Termination (Let's Encrypt / Custom Cert)     │
│ - Security Headers (HSTS, CSP, X-Frame-Options: DENY)       │
│ - Rate Limiting: 15 req/min (/confirm-balance/), 100 req/min │
└────────────────────────────┬────────────────────────────────┘
                             │ (Loopback 127.0.0.1:8000)
                             ▼
┌─────────────────────────────────────────────────────────────┐
│ Daily Close Web Application (Uvicorn / FastAPI)             │
│ - 4 Async Workers                                           │
│ - Strict Least-Privilege App Role (NOBYPASSRLS)             │
│ - Integer Santim Money & Evidence Ladder (Rule 2)           │
└────────────────────────────┬────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────┐
│ PostgreSQL 16 Database Server                               │
│ - Forced Row-Level Security (RLS) on every tenant table     │
│ - Append-Only Immutable Event Ledger (REVOKE UPDATE/DELETE) │
│ - Separate Roles: postgres, app, backup_role, auth_lookup   │
└─────────────────────────────────────────────────────────────┘
```

### Non-Negotiable Operational Invariants
1. **Rule 7 (Sovereign Data Residency):** All financial data, customer records, and event logs remain strictly on local Ethiopian infrastructure. No cloud telemetry or third-party external SDK calls.
2. **Rule 8 (Forced Row-Level Security):** Every domain query executes under PostgreSQL role `app` with `FORCE ROW LEVEL SECURITY`.
3. **Rule 4 (Append-Only Event Ledger):** Database permissions strictly enforce `REVOKE UPDATE, DELETE ON events FROM app`. Any operational adjustment requires an auditable compensating event.
4. **SEC-01 (Air-Gapped Backup Custody):** Automated nightly backups are encrypted on the server with `age` using an asymmetric **public key only**. The private decryption key is kept strictly offline in physical hardware custody.

---

## 2. Server Prerequisites & Package Installation

Target host: Clean installation of **Ubuntu 22.04 LTS** or **Debian 12** on dedicated on-premise hardware or local VPS in Addis Ababa.

### Step 2.1: System Packages Installation
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    libpq-dev \
    nginx \
    certbot \
    python3-certbot-nginx \
    age \
    ufw

# Install Python 3.12
sudo add-apt-repository ppa:deadsnakes/ppa -y
sudo apt update
sudo apt install -y python3.12 python3.12-venv python3.12-dev
```

### Step 2.2: Dedicated Service User Creation
```bash
# Create system user and group with no login shell
sudo groupadd -g 10001 dailyclose
sudo useradd -u 10001 -g dailyclose -s /bin/false -d /opt/dailyclose -m dailyclose

# Create operational directories with strict permissions
sudo mkdir -p /opt/dailyclose
sudo mkdir -p /var/backups/dailyclose
sudo mkdir -p /var/log/dailyclose
sudo mkdir -p /etc/dailyclose

sudo chown -R dailyclose:dailyclose /opt/dailyclose
sudo chown -R dailyclose:dailyclose /var/backups/dailyclose
sudo chown -R dailyclose:dailyclose /var/log/dailyclose
sudo chown -R dailyclose:dailyclose /etc/dailyclose

sudo chmod 750 /opt/dailyclose
sudo chmod 700 /var/backups/dailyclose
sudo chmod 750 /var/log/dailyclose
sudo chmod 750 /etc/dailyclose
```

### Step 2.3: Firewall (UFW) Configuration
```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp comment 'SSH'
sudo ufw allow 80/tcp comment 'HTTP (Certbot/ACME)'
sudo ufw allow 443/tcp comment 'HTTPS'
sudo ufw enable
```

---

## 3. PostgreSQL 16 Database Configuration & Security Hardening

### Step 3.1: Provision Database Roles & Permissions
Connect to PostgreSQL as administrative `postgres` user:
```bash
sudo -u postgres psql
```

Execute the hardened role-segregation script:
```sql
-- 1. Create unprivileged application role (NOBYPASSRLS)
CREATE ROLE app WITH LOGIN PASSWORD 'YOUR_STRONG_APP_PASSWORD_HERE' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;

-- 2. Create dedicated backup role (BYPASSRLS, read-only)
CREATE ROLE backup_role WITH LOGIN PASSWORD 'YOUR_STRONG_BACKUP_PASSWORD_HERE' NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS;

-- 3. Create dedicated pre-auth lookup role
CREATE ROLE auth_lookup_role WITH LOGIN PASSWORD 'YOUR_STRONG_AUTH_LOOKUP_PASSWORD_HERE' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;

-- 4. Create production database owned by postgres
CREATE DATABASE dailyclose OWNER postgres;

-- 5. Grant access privileges
\c dailyclose

GRANT CONNECT ON DATABASE dailyclose TO app;
GRANT CONNECT ON DATABASE dailyclose TO backup_role;
GRANT CONNECT ON DATABASE dailyclose TO auth_lookup_role;

GRANT USAGE ON SCHEMA public TO app;
GRANT USAGE ON SCHEMA public TO backup_role;
GRANT USAGE ON SCHEMA public TO auth_lookup_role;

\q
```

---

## 4. Environment Configuration & Cryptographic Secrets

### Step 4.1: Generate Cryptographic Secrets
Run the following on your workstation or host:
```bash
# 1. Generate 64-character hex SECRET_KEY for session & approval tokens
python3.12 -c "import secrets; print(secrets.token_hex(32))"

# 2. Generate age asymmetric keypair for offsite backups
age-keygen -o offline_identity.txt
# Output displays public key: age1... (store this public key in /etc/dailyclose/age_recipients.txt)
# IMPORTANT: Move offline_identity.txt to an encrypted USB drive in physical custody. Delete from server.
```

### Step 4.2: Create `/opt/dailyclose/.env`
Create `/opt/dailyclose/.env` (permissions `0600`, owned by `dailyclose:dailyclose`):
```ini
# /opt/dailyclose/.env
APP_ENV=production
LOG_LEVEL=INFO

# Application Database Connection (app role, RLS enforced)
DATABASE_URL=postgresql+psycopg://app:YOUR_STRONG_APP_PASSWORD_HERE@localhost:5432/dailyclose

# Migration Database Connection (postgres superuser)
DATABASE_MIGRATION_URL=postgresql+psycopg://postgres:YOUR_SUPERUSER_PASSWORD_HERE@localhost:5432/dailyclose

# Security & Session Token Secret
SECRET_KEY=PASTE_GENERATED_64_CHAR_HEX_KEY_HERE

# Backup Recipient Public Key File
AGE_RECIPIENTS_FILE=/etc/dailyclose/age_recipients.txt

# Trusted Reverse Proxy IP (defends against X-Forwarded-For spoofing)
TRUSTED_PROXY_IP=127.0.0.1

# Telegram Bot (Optional, if using staff bot)
TELEGRAM_BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN_HERE
```

Store the `age` public key:
```bash
echo "age1YOUR_PUBLIC_KEY_HERE" | sudo tee /etc/dailyclose/age_recipients.txt
sudo chown dailyclose:dailyclose /etc/dailyclose/age_recipients.txt
sudo chmod 644 /etc/dailyclose/age_recipients.txt
```

---

## 5. Application Code Setup & Initial Bootstrapping

### Step 5.1: Clone Repository & Virtual Environment
```bash
sudo -u dailyclose git clone https://github.com/birana-tech/daily-close.git /opt/dailyclose
cd /opt/dailyclose

# Create Python 3.12 virtual environment
sudo -u dailyclose python3.12 -m venv /opt/dailyclose/.venv
sudo -u dailyclose /opt/dailyclose/.venv/bin/pip install --upgrade pip
sudo -u dailyclose /opt/dailyclose/.venv/bin/pip install -r requirements/prod.txt
```

### Step 5.2: Run Database Migrations
Apply all schema migrations up to head (`0010_pilot_metrics`):
```bash
sudo -u dailyclose /opt/dailyclose/.venv/bin/alembic upgrade head
```

### Step 5.3: Bootstrap Initial Wholesale Tenant & Owner
```bash
sudo -u dailyclose /opt/dailyclose/.venv/bin/python -m app.cli bootstrap-owner \
    --tenant "Addis Wholesale Trading" \
    --identifier "owner@addiswholesale.et" \
    --name "Ato Owner"
# Prompts for secure password (minimum 10 characters)
```

---

## 6. Service Management (Systemd & Nginx)

### Step 6.1: Install Systemd Services
Copy the production unit files from `deploy/`:
```bash
sudo cp deploy/dailyclose-web.service /etc/systemd/system/
sudo cp deploy/dailyclose-bot.service /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable dailyclose-web
sudo systemctl enable dailyclose-bot

sudo systemctl start dailyclose-web
sudo systemctl start dailyclose-bot
```

Verify service status:
```bash
sudo systemctl status dailyclose-web
curl -f http://127.0.0.1:8000/health
# Returns: {"status": "healthy"}
```

### Step 6.2: Configure Nginx & TLS
Copy `deploy/nginx.conf` into place:
```bash
sudo cp deploy/nginx.conf /etc/nginx/nginx.conf
sudo nginx -t
```

Obtain Let's Encrypt TLS certificate:
```bash
sudo certbot --nginx -d dailyclose.birana.et
sudo systemctl reload nginx
```

---

## 7. Nightly Encrypted Backups & Disaster Recovery

### Step 7.1: Configure Crontab
Configure daily automated encrypted backups at 02:00 AM UTC:
```bash
sudo crontab -u dailyclose -e
```
Add line:
```cron
0 2 * * * /opt/dailyclose/deploy/backup_nightly.sh >> /var/log/dailyclose/backup.log 2>&1
```

### Step 7.2: Run Manual Backup Test
```bash
sudo -u dailyclose /opt/dailyclose/deploy/backup_nightly.sh
```
Verify generated `.age` archive and `.sha256` checksum in `/var/backups/dailyclose/`.

### Step 7.3: Disaster Recovery Restore Drill
To verify that backups can be successfully restored from bare metal:
```bash
# Test decryption and restore into a scratch database
sudo -u postgres createdb scratch_restore_test
sudo deploy/restore_backup.sh \
    /var/backups/dailyclose/dailyclose_backup_YYYYMMDD_HHMMSSZ.age \
    /path/to/offline_identity.txt \
    scratch_restore_test

sudo -u postgres dropdb scratch_restore_test
```

---

## 8. Post-Deployment Verification & Smoke Tests

Run the comprehensive release gate check on the production machine:
```bash
sudo -u dailyclose /opt/dailyclose/.venv/bin/python -m app.cli release-gate
```

### Operational Smoke Test Checklist
- [ ] **HTTP to HTTPS Redirect:** Visiting `http://dailyclose.birana.et` redirects to `https://dailyclose.birana.et` with status 301.
- [ ] **Security Headers:** Verify `Strict-Transport-Security`, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, and Content Security Policy (`CSP`) headers are returned.
- [ ] **Owner Authentication:** Log in as owner at `/login` with seeded credentials.
- [ ] **Credit Sale Entry:** Record a test credit sale and verify sequence gap warnings.
- [ ] **Statement Import Wizard:** Open `/statements/wizard`, upload a synthetic CBE CSV, verify column detection and 20-row preview.
- [ ] **Morning Collections Brief:** Verify `/brief` calculates overdue brackets and priority call list.
- [ ] **Buyer Balance Confirmation:** Generate a confirmation token for a retailer, load `/confirm-balance/{token}` in an incognito tab, verify rate limiting and `retailer_token.viewed` event logging.
- [ ] **Overdue Reminders:** Open `/reminders`, view overdue accounts, and test compose dialog.
- [ ] **Audit Baseline Snapshot:** Execute `python -m app.cli pilot-baseline --tenant-id <UUID>` and verify snapshot storage.

---

## 9. Operational Troubleshooting & Emergency Procedures

| Issue | Diagnostic Command | Remediation Action |
| :--- | :--- | :--- |
| **Account Locked (Rate Limiting)** | `grep "locked" /var/log/dailyclose/*.log` | Run `python -m app.cli unlock-user --identifier user@email.et` to reset failed attempts. |
| **Alembic Migration Error** | `alembic current` vs `alembic heads` | Check database superuser connection string in `DATABASE_MIGRATION_URL`. |
| **Nginx 429 Too Many Requests** | Check `/var/log/nginx/dailyclose.error.log` | Retailer or staff exceeded 15 req/min burst on confirmation links or 100 req/min general rate limit. |
| **Web Service Crash / Startup Failure** | `journalctl -u dailyclose-web -n 50 --no-pager` | Verify `.env` has `APP_ENV=production`, `SECRET_KEY` $\ge 64$ chars, and database is reachable on 127.0.0.1:5432. |
| **Internet / Telecom Outage** | Physical market disconnection | Daily Close is hosted on an internal local network in Addis Ababa; cashiers continue recording sales locally; statement imports resume once bank network recovers. |
