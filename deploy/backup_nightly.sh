#!/usr/bin/env bash
# ==============================================================================
# deploy/backup_nightly.sh
# Nightly Encrypted PostgreSQL Backup for Daily Close (Birana Technology)
# Target: Single Linux Server hosted in Addis Ababa, Ethiopia
#
# Non-Negotiable Rule 7 (Data Residency):
#   Backups stay strictly on the local host or an Ethiopian-sovereign storage unit.
#   Zero external/cloud SDK telemetry.
#
# SEC-01 & Cryptography:
#   Runs under dedicated read-only 'backup_role' with BYPASSRLS (never app role or superuser).
#   Encrypted via 'age' using asymmetric public-key mode (X25519 + ChaCha20-Poly1305 AEAD).
#   The server holds ONLY the public recipient key; the private decryption key is kept strictly offline.
#
# Recommended Crontab entry:
#   0 2 * * * /opt/dailyclose/deploy/backup_nightly.sh >> /var/log/dailyclose/backup.log 2>&1
# ==============================================================================

set -euo pipefail

# Configuration
BACKUP_DIR="${BACKUP_DIR:-/var/backups/dailyclose}"
LOG_FILE="${LOG_FILE:-/var/log/dailyclose/backup.log}"
ENV_FILE="${ENV_FILE:-/opt/dailyclose/.env}"
AGE_RECIPIENTS_FILE="${AGE_RECIPIENTS_FILE:-/opt/dailyclose/backup_pubkey.txt}"
RETENTION_DAYS=30
TIMESTAMP=$(date -u +"%Y%m%d_%H%M%SZ")
DATE_LOG=$(date -u +"%Y-%m-%d %H:%M:%S UTC")

echo "======================================================================"
echo "[${DATE_LOG}] Starting Daily Close Nightly Encrypted Backup..."

# 1. Load Environment Configuration
if [[ -f "${ENV_FILE}" ]]; then
    # shellcheck disable=SC1090
    set -a
    source "${ENV_FILE}"
    set +a
fi

DB_NAME="${POSTGRES_DB:-dailyclose}"
# SEC-01: Must run under dedicated backup_role with BYPASSRLS
DB_USER="${BACKUP_DB_USER:-backup_role}"
DB_HOST="${POSTGRES_HOST:-localhost}"
DB_PORT="${POSTGRES_PORT:-5432}"

# 2. Ensure Backup Directory Exists with Secure Permissions (0700)
mkdir -p "${BACKUP_DIR}"
chmod 700 "${BACKUP_DIR}"

DUMP_TMP="${BACKUP_DIR}/dump_${DB_NAME}_${TIMESTAMP}.tmp"
ENC_BACKUP="${BACKUP_DIR}/dailyclose_backup_${TIMESTAMP}.age"
SHA_FILE="${BACKUP_DIR}/dailyclose_backup_${TIMESTAMP}.sha256"

# Cleanup trap for temporary plaintext dump
cleanup() {
    if [[ -f "${DUMP_TMP}" ]]; then
        rm -f "${DUMP_TMP}"
    fi
}
trap cleanup EXIT INT TERM

# 3. Create PostgreSQL Dump (Custom format, compressed)
echo "[INFO] Dumping PostgreSQL database '${DB_NAME}' as user '${DB_USER}'..."
PGPASSWORD="${BACKUP_DB_PASSWORD:-${POSTGRES_PASSWORD:-}}" pg_dump \
    -h "${DB_HOST}" \
    -p "${DB_PORT}" \
    -U "${DB_USER}" \
    -d "${DB_NAME}" \
    -F c \
    -b \
    -f "${DUMP_TMP}"

DUMP_SIZE=$(stat -c%s "${DUMP_TMP}" 2>/dev/null || stat -f%z "${DUMP_TMP}" 2>/dev/null || echo "0")
echo "[INFO] Database dump created successfully (${DUMP_SIZE} bytes)."

# 4. Encrypt Dump with age in Public-Key Mode (Authenticated Encryption)
echo "[INFO] Encrypting dump with age public-key encryption..."
if [[ -f "${AGE_RECIPIENTS_FILE}" ]]; then
    age -R "${AGE_RECIPIENTS_FILE}" -o "${ENC_BACKUP}" "${DUMP_TMP}"
elif [[ -n "${AGE_RECIPIENT:-}" ]]; then
    age -r "${AGE_RECIPIENT}" -o "${ENC_BACKUP}" "${DUMP_TMP}"
else
    echo "[ERROR] Neither AGE_RECIPIENTS_FILE (${AGE_RECIPIENTS_FILE}) nor AGE_RECIPIENT is available." >&2
    exit 1
fi

chmod 600 "${ENC_BACKUP}"

# 5. Generate SHA-256 Checksum for Integrity Verification
echo "[INFO] Generating SHA-256 integrity checksum..."
sha256sum "${ENC_BACKUP}" > "${SHA_FILE}"
chmod 600 "${SHA_FILE}"

ENC_SIZE=$(stat -c%s "${ENC_BACKUP}" 2>/dev/null || stat -f%z "${ENC_BACKUP}" 2>/dev/null || echo "0")
echo "[SUCCESS] Encrypted backup stored at: ${ENC_BACKUP} (${ENC_SIZE} bytes)"
echo "[SUCCESS] Integrity hash: $(cat "${SHA_FILE}")"

# 6. Apply Retention Policy (Delete backups older than RETENTION_DAYS)
echo "[INFO] Purging backups older than ${RETENTION_DAYS} days..."
find "${BACKUP_DIR}" -type f \( -name "*.age" -o -name "*.sha256" \) -mtime "+${RETENTION_DAYS}" -exec rm -f {} +

echo "[${DATE_LOG}] Backup process completed successfully."
echo "======================================================================"
