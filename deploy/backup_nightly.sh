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
# Recommended Crontab entry:
#   0 2 * * * /opt/dailyclose/deploy/backup_nightly.sh >> /var/log/dailyclose/backup.log 2>&1
# ==============================================================================

set -euo pipefail

# Configuration
BACKUP_DIR="${BACKUP_DIR:-/var/backups/dailyclose}"
LOG_FILE="${LOG_FILE:-/var/log/dailyclose/backup.log}"
ENV_FILE="${ENV_FILE:-/opt/dailyclose/.env}"
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
else
    echo "[ERROR] Environment file ${ENV_FILE} not found." >&2
    exit 1
fi

# Ensure mandatory variables exist
if [[ -z "${BACKUP_PASSPHRASE:-}" ]]; then
    echo "[ERROR] BACKUP_PASSPHRASE is not set in environment or ${ENV_FILE}." >&2
    exit 1
fi

DB_NAME="${POSTGRES_DB:-dailyclose}"
DB_USER="${POSTGRES_USER:-app}"
DB_HOST="${POSTGRES_HOST:-localhost}"
DB_PORT="${POSTGRES_PORT:-5432}"

# 2. Ensure Backup Directory Exists with Secure Permissions (0700)
mkdir -p "${BACKUP_DIR}"
chmod 700 "${BACKUP_DIR}"

DUMP_TMP="${BACKUP_DIR}/dump_${DB_NAME}_${TIMESTAMP}.tmp"
ENC_BACKUP="${BACKUP_DIR}/dailyclose_backup_${TIMESTAMP}.enc"
SHA_FILE="${BACKUP_DIR}/dailyclose_backup_${TIMESTAMP}.sha256"

# Cleanup trap for temporary plaintext dump
cleanup() {
    if [[ -f "${DUMP_TMP}" ]]; then
        rm -f "${DUMP_TMP}"
    fi
}
trap cleanup EXIT INT TERM

# 3. Create PostgreSQL Dump (Custom format, compressed)
echo "[INFO] Dumping PostgreSQL database '${DB_NAME}'..."
PGPASSWORD="${POSTGRES_PASSWORD:-${DB_PASSWORD:-}}" pg_dump \
    -h "${DB_HOST}" \
    -p "${DB_PORT}" \
    -U "${DB_USER}" \
    -d "${DB_NAME}" \
    -F c \
    -b \
    -f "${DUMP_TMP}"

DUMP_SIZE=$(stat -c%s "${DUMP_TMP}" 2>/dev/null || stat -f%z "${DUMP_TMP}" 2>/dev/null || echo "0")
echo "[INFO] Database dump created successfully (${DUMP_SIZE} bytes)."

# 4. Encrypt Dump with OpenSSL AES-256-CBC (PBKDF2, 100,000 iterations)
echo "[INFO] Encrypting dump with OpenSSL AES-256-CBC..."
openssl enc -aes-256-cbc \
    -salt \
    -pbkdf2 \
    -iter 100000 \
    -in "${DUMP_TMP}" \
    -out "${ENC_BACKUP}" \
    -pass env:BACKUP_PASSPHRASE

chmod 600 "${ENC_BACKUP}"

# 5. Generate SHA-256 Checksum for Integrity Verification
echo "[INFO] Generating SHA-256 integrity checksum..."
sha256sum "${ENC_BACKUP}" > "${SHA_FILE}"
chmod 600 "${SHA_FILE}"

ENC_SIZE=$(stat -c%s "${ENC_BACKUP}" 2>/dev/null || stat -f%z "${ENC_BACKUP}" 2>/dev/null || echo "0")
echo "[SUCCESS] Encrypted backup stored at: ${ENC_BACKUP} (${ENC_SIZE} bytes)"
echo "[SUCCESS] Integrity hash: $(cat "${SHA_FILE}")"

# 6. Delete Plaintext Dump Immediately
rm -f "${DUMP_TMP}"

# 7. Prune Backups Older Than 30 Days
echo "[INFO] Pruning backups older than ${RETENTION_DAYS} days..."
DELETED_ENC=$(find "${BACKUP_DIR}" -type f -name "dailyclose_backup_*.enc" -mtime +"${RETENTION_DAYS}" -print -delete | wc -l)
DELETED_SHA=$(find "${BACKUP_DIR}" -type f -name "dailyclose_backup_*.sha256" -mtime +"${RETENTION_DAYS}" -print -delete | wc -l)
echo "[INFO] Pruned ${DELETED_ENC} old encrypted backup file(s) and ${DELETED_SHA} checksum file(s)."

echo "[$(date -u +"%Y-%m-%d %H:%M:%S UTC")] Nightly backup finished successfully."
echo "======================================================================"
