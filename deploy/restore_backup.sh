#!/usr/bin/env bash
# ==============================================================================
# deploy/restore_backup.sh
# Decrypt and Restore Encrypted PostgreSQL Backup for Daily Close
# Birana Technology — Addis Ababa, Ethiopia
#
# Usage:
#   ./deploy/restore_backup.sh <path_to_encrypted_backup.age> <path_to_offline_private_key.txt> [target_db]
# ==============================================================================

set -euo pipefail

if [[ $# -lt 2 ]]; then
    echo "Usage: $0 <path_to_encrypted_backup.age> <path_to_offline_identity_file> [target_database_name]" >&2
    exit 1
fi

ENC_FILE="$1"
IDENTITY_FILE="$2"
TARGET_DB="${3:-dailyclose}"
ENV_FILE="${ENV_FILE:-/opt/dailyclose/.env}"

if [[ ! -f "${ENC_FILE}" ]]; then
    echo "[ERROR] Encrypted backup file '${ENC_FILE}' does not exist." >&2
    exit 1
fi

if [[ ! -f "${IDENTITY_FILE}" ]]; then
    echo "[ERROR] Offline identity (private key) file '${IDENTITY_FILE}' does not exist." >&2
    exit 1
fi

# 1. Load Environment Configuration
if [[ -f "${ENV_FILE}" ]]; then
    # shellcheck disable=SC1090
    set -a
    source "${ENV_FILE}"
    set +a
fi

# 2. Check Integrity Hash if available
SHA_FILE="${ENC_FILE%.*}.sha256"
if [[ -f "${SHA_FILE}" ]]; then
    echo "[INFO] Verifying SHA-256 integrity checksum..."
    sha256sum --check "${SHA_FILE}"
    echo "[INFO] Checksum verified: OK"
else
    echo "[WARN] No SHA-256 checksum file found at ${SHA_FILE}."
fi

RESTORE_TMP="${ENC_FILE%.*}.restore.tmp"

cleanup() {
    if [[ -f "${RESTORE_TMP}" ]]; then
        rm -f "${RESTORE_TMP}"
    fi
}
trap cleanup EXIT INT TERM

# 3. Decrypt Backup File with age (Authenticated AEAD Decryption)
echo "[INFO] Decrypting '${ENC_FILE}' using offline private key..."
age -d -i "${IDENTITY_FILE}" -o "${RESTORE_TMP}" "${ENC_FILE}"

echo "[INFO] Backup decrypted and integrity verified successfully."

# 4. Restore Database via pg_restore
DB_USER="${POSTGRES_USER:-postgres}"
DB_HOST="${POSTGRES_HOST:-localhost}"
DB_PORT="${POSTGRES_PORT:-5432}"

echo "[WARNING] Restoring into database '${TARGET_DB}' on ${DB_HOST}:${DB_PORT} as '${DB_USER}'..."

PGPASSWORD="${POSTGRES_PASSWORD:-${DB_PASSWORD:-}}" pg_restore \
    -h "${DB_HOST}" \
    -p "${DB_PORT}" \
    -U "${DB_USER}" \
    -d "${TARGET_DB}" \
    --clean \
    --if-exists \
    --verbose \
    "${RESTORE_TMP}"

echo "[INFO] Verifying table counts and sequence values in restored database..."
PGPASSWORD="${POSTGRES_PASSWORD:-${DB_PASSWORD:-}}" psql \
    -h "${DB_HOST}" \
    -p "${DB_PORT}" \
    -U "${DB_USER}" \
    -d "${TARGET_DB}" \
    -c "SELECT count(*) AS total_tenants FROM tenants;" \
    -c "SELECT count(*) AS total_users FROM users;" \
    -c "SELECT count(*) AS total_events FROM events;" \
    -c "SELECT sequence_name, last_value FROM information_schema.sequences;"

echo "[SUCCESS] Database '${TARGET_DB}' restored and verified successfully from '${ENC_FILE}'."
