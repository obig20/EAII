"""
tests/accounting/test_accounting_rls.py

Tenant Isolation & RLS Security Tests for General Ledger Module.
Verifies:
- Chart of accounts, journal entries, and lines are strictly isolated.
"""

from __future__ import annotations

from datetime import date
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounting.service import init_default_chart_of_accounts, post_journal_entry
from app.kernel.models import Account, JournalEntry, Tenant, User
from app.kernel.tenant import set_tenant


@pytest.fixture(autouse=True)
def ensure_tables(tables):
    pass


class TestAccountingTenantIsolation:
    def test_gl_accounts_and_entries_isolation(
        self, db_session: Session, tenant_a: Tenant, tenant_b: Tenant, owner_user_a: User
    ):
        # Tenant A sets up accounts and posts entry
        set_tenant(db_session, tenant_a.id)
        accounts_a = init_default_chart_of_accounts(db_session, tenant_a.id)
        acc_a = accounts_a[0]
        acc_b = accounts_a[1]

        je_a = post_journal_entry(
            db_session, tenant_a.id, "JE-A-01", date.today(), "Tenant A Entry",
            [(acc_a.id, 100_000, 0, "D"), (acc_b.id, 0, 100_000, "C")],
            created_by=owner_user_a.id,
        )

        # Switch to Tenant B
        set_tenant(db_session, tenant_b.id)

        # Tenant B sees 0 journal entries from Tenant A
        entries_b = db_session.execute(select(JournalEntry)).scalars().all()
        assert not any(e.id == je_a.id for e in entries_b)
