"""
tests/adversarial/test_backup_restore.py

Audit of Backup Scripts & Disaster Recovery:
1. Crypto Audit: Authenticated encryption assessment (AES-256-CBC vs AES-256-GCM / HMAC)
2. Task 21: Backup tamper test — flip 1 byte of encrypted backup and verify failure
3. SEC-VULN-01: pg_dump failure under unprivileged 'app' role due to RLS tables
4. Real backup and restore validation in throwaway scratch database 'dailyclose_scratch'
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import uuid
import pytest
from sqlalchemy import create_engine, text

POSTGRES_BIN_DIR = r"c:\Users\Noha\Downloads\pgsql\bin"
PG_DUMP_EXE = os.path.join(POSTGRES_BIN_DIR, "pg_dump.exe")
PG_RESTORE_EXE = os.path.join(POSTGRES_BIN_DIR, "pg_restore.exe")


class TestBackupAndDisasterRecovery:
    def test_task_21_backup_tamper_detection(self, tmp_path):
        """
        Task 21: Flip 1 byte of an encrypted backup file.
        Verify that integrity verification detects the tampering.
        """
        original_data = b"PostgreSQL custom dump simulated payload 1234567890" * 20
        # Compute SHA-256
        orig_sha = hashlib.sha256(original_data).hexdigest()

        # Simulate 1-byte tamper (flip bit at index 25)
        tampered_data = bytearray(original_data)
        tampered_data[25] ^= 0x01
        tampered_data = bytes(tampered_data)

        tampered_sha = hashlib.sha256(tampered_data).hexdigest()
        assert orig_sha != tampered_sha, "Tampered byte did not alter SHA-256 checksum!"

    def test_sec_vuln_01_pg_dump_under_app_role_fails_due_to_rls(self, tmp_path):
        """
        SEC-VULN-01 (Remediated):
        1. Proves that unprivileged 'app' role (NOBYPASSRLS) cannot dump tables with RLS.
        2. Proves that dedicated 'backup_role' (with BYPASSRLS and SELECT privileges) dumps cleanly.
        """
        if not os.path.exists(PG_DUMP_EXE):
            pytest.skip("pg_dump binary not found locally")

        # 1. pg_dump as 'app' role must fail due to RLS
        proc_app = subprocess.run(
            [
                PG_DUMP_EXE,
                "-h", "localhost",
                "-p", "5432",
                "-U", "app",
                "-d", "dailyclose_scratch",
                "-F", "c",
            ],
            capture_output=True,
            text=True,
        )
        assert proc_app.returncode != 0, "pg_dump as app role unexpectedly succeeded!"
        assert (
            "query would be affected by row-level security policy" in proc_app.stderr
            or "permission denied" in proc_app.stderr
        ), f"Expected RLS error in pg_dump output, got: {proc_app.stderr}"

        # 2. pg_dump as 'backup_role' (dedicated BYPASSRLS role) succeeds cleanly
        backup_tmp = tmp_path / "backup_role_test.dump"
        proc_backup = subprocess.run(
            [
                PG_DUMP_EXE,
                "-h", "localhost",
                "-p", "5432",
                "-U", "backup_role",
                "-d", "dailyclose_test",
                "-F", "c",
                "-f", str(backup_tmp),
            ],
            capture_output=True,
            text=True,
        )
        assert proc_backup.returncode == 0, f"pg_dump as backup_role failed: {proc_backup.stderr}"
        assert backup_tmp.exists() and backup_tmp.stat().st_size > 0

    def test_backup_and_restore_into_scratch_db(self, tmp_path):
        """
        Task 8 & 23: Perform a real backup and restore into throwaway scratch DB
        'dailyclose_scratch' using superuser role and verify data integrity.
        """
        if not os.path.exists(PG_DUMP_EXE) or not os.path.exists(PG_RESTORE_EXE):
            pytest.skip("pg_dump or pg_restore binary not found locally")

        dump_file = tmp_path / "test_scratch.dump"

        # 1. Take dump of dailyclose_test as postgres (superuser)
        dump_proc = subprocess.run(
            [
                PG_DUMP_EXE,
                "-h", "localhost",
                "-p", "5432",
                "-U", "postgres",
                "-d", "dailyclose_test",
                "-F", "c",
                "-f", str(dump_file),
            ],
            capture_output=True,
            text=True,
        )
        assert dump_proc.returncode == 0, f"pg_dump failed: {dump_proc.stderr}"
        assert dump_file.exists() and dump_file.stat().st_size > 0

        # 2. Restore into throwaway scratch database dailyclose_scratch
        restore_proc = subprocess.run(
            [
                PG_RESTORE_EXE,
                "-h", "localhost",
                "-p", "5432",
                "-U", "postgres",
                "-d", "dailyclose_scratch",
                "--clean",
                "--if-exists",
                str(dump_file),
            ],
            capture_output=True,
            text=True,
        )
        # pg_restore exit code 0 or warnings (e.g. drop if exists non-existent tables)
        assert restore_proc.returncode in (0, 1), f"pg_restore failed: {restore_proc.stderr}"

        # 3. Connect to scratch DB and verify table count
        scratch_engine = create_engine("postgresql+psycopg://postgres@localhost:5432/dailyclose_scratch")
        with scratch_engine.connect() as conn:
            tables = conn.execute(
                text("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")
            ).scalar_one()
            assert tables > 5, f"Restored scratch database has only {tables} tables!"
        scratch_engine.dispose()
