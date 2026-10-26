#!/usr/bin/env bash
# ==============================================================================
# deploy/restore_backup.sh
# Decrypt and Restore Encrypted PostgreSQL Backup for Daily Close
# Birana Technology — Addis Ababa, Ethiopia
#
# Usage:
#   ./deploy/restore_backup.sh /var/backups/dailyclose/dailyclose_backup_YYYYMMDD_HHMMSSZ.enc [target_db]
# ==============================================================================

set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <path_to_encrypted_backup_file> [target_database_name]" >&2
    exit 1
fi

ENC_FILE="$1"
TARGET_DB="${2:-dailyclose}"
ENV_FILE="${ENV_FILE:-/opt/dailyclose/.env}"

if [[ ! -f "${ENC_FILE}" ]]; then
    echo "[ERROR] Encrypted backup file '${ENC_FILE}' does not exist." >&2
    exit 1
fi

# 1. Load Environment Configuration
if [[ -f "${ENV_FILE}" ]]; then
    # shellcheck disable=SC1090
    set -a
    source "${ENV_FILE}"
    set +a
fi

if [[ -z "${BACKUP_PASSPHRASE:-}" ]]; then
    read -r -s -p "Enter BACKUP_PASSPHRASE: " BACKUP_PASSPHRASE
    echo
    export BACKUP_PASSPHRASE
fi

# 2. Check Integrity Hash if available
SHA_FILE="${ENC_FILE%.*}.sha256"
if [[ -f "${SHA_FILE}" ]]; then
    echo "[INFO] Verifying SHA-256 integrity checksum..."
    sha256sum --check "${SHA_FILE}"
    echo "[INFO] Checksum verified: OK"
else
    echo "[WARN] No SHA-256 checksum file found at ${SHA_FILE}. Proceeding with cautious restore."
fi

RESTORE_TMP="${ENC_FILE%.*}.restore.tmp"

cleanup() {
    if [[ -f "${RESTORE_TMP}" ]]; then
        rm -f "${RESTORE_TMP}"
    fi
}
trap cleanup EXIT INT TERM

# 3. Decrypt Backup File with OpenSSL
echo "[INFO] Decrypting '${ENC_FILE}'..."
openssl enc -d -aes-256-cbc \
    -pbkdf2 \
    -iter 100000 \
    -in "${ENC_FILE}" \
    -out "${RESTORE_TMP}" \
    -pass env:BACKUP_PASSPHRASE

echo "[INFO] Backup decrypted successfully."

# 4. Restore Database via pg_restore
DB_USER="${POSTGRES_USER:-postgres}"
DB_HOST="${POSTGRES_HOST:-localhost}"
DB_PORT="${POSTGRES_PORT:-5432}"

echo "[WARNING] Restoring into database '${TARGET_DB}' on ${DB_HOST}:${DB_PORT} as '${DB_USER}'..."
read -r -p "Are you sure you want to proceed? [y/N] " CONFIRM
if [[ "${CONFIRM}" != "y" && "${CONFIRM}" != "Y" ]]; then
    echo "[INFO] Restore cancelled by user."
    exit 0
fi

PGPASSWORD="${POSTGRES_PASSWORD:-${DB_PASSWORD:-}}" pg_restore \
    -h "${DB_HOST}" \
    -p "${DB_PORT}" \
    -U "${DB_USER}" \
    -d "${TARGET_DB}" \
    --clean \
    --if-exists \
    --verbose \
    "${RESTORE_TMP}"

echo "[SUCCESS] Database '${TARGET_DB}' restored successfully from '${ENC_FILE}'."
