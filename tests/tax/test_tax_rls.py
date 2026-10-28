"""
tests/tax/test_tax_rls.py

Tenant Isolation & RLS Security Tests for Tax Module.
Verifies:
- Withholding receipts are strictly tenant-isolated under the unprivileged 'app' role.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.kernel.models import Tenant, User, WithholdingReceipt
from app.kernel.tenant import set_tenant
from app.tax.service import issue_withholding_receipt


@pytest.fixture(autouse=True)
def ensure_tables(tables):
    pass


class TestTaxTenantIsolation:
    def test_withholding_receipt_tenant_isolation(
        self, db_session: Session, tenant_a: Tenant, tenant_b: Tenant, owner_user_a: User
    ):
        set_tenant(db_session, tenant_a.id)
        rcpt_a = issue_withholding_receipt(
            db_session, tenant_a.id, "WHT-TENANT-A", "supplier",
            "Metehara Sugar", "0099112233", "INV-M-01", 1_500_000,
            created_by=owner_user_a.id,
        )

        # Switch to Tenant B
        set_tenant(db_session, tenant_b.id)
        rcpts_b = db_session.execute(select(WithholdingReceipt)).scalars().all()
        assert not any(r.id == rcpt_a.id for r in rcpts_b)
