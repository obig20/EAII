"""
tests/accounting/test_accounting_service.py

Test Suite for ERP Double-Entry General Ledger & Peachtree Sync.
Verifies:
- Standard Chart of Accounts initialization
- Double-entry mathematical balance enforcement (Debits == Credits)
- Imbalance guard rejection (DoubleEntryImbalanceError)
- Trial balance calculation and balance check
- Profit and Loss calculation
- Peachtree / Sage 50 General Journal export
"""

from __future__ import annotations

from datetime import date
import pytest
from sqlalchemy.orm import Session

from app.accounting.service import (
    DoubleEntryImbalanceError,
    export_peachtree_journal_csv,
    get_profit_and_loss,
    get_trial_balance,
    init_default_chart_of_accounts,
    post_journal_entry,
)
from app.kernel.models import Tenant, User
from app.kernel.tenant import set_tenant


@pytest.fixture(autouse=True)
def ensure_tables(tables):
    pass


class TestAccountingService:
    def test_chart_of_accounts_and_balanced_journal_entry(
        self, db_session: Session, tenant_a: Tenant, owner_user_a: User
    ):
        set_tenant(db_session, tenant_a.id)

        # 1. Initialize Chart of Accounts
        accounts = init_default_chart_of_accounts(db_session, tenant_a.id)
        assert len(accounts) >= 10
        acc_dict = {a.account_code: a for a in accounts}

        # 2. Post balanced cash sale: Dr Cash 1010, Cr Revenue 4000 (5,000 ETB = 500,000 santim)
        je = post_journal_entry(
            session=db_session,
            tenant_id=tenant_a.id,
            entry_number="JE-2026-001",
            entry_date=date.today(),
            description="Cash sale at Merkato store",
            lines_data=[
                (acc_dict["1010"].id, 500_000, 0, "Cash in register"),
                (acc_dict["4000"].id, 0, 500_000, "Wholesale sales revenue"),
            ],
            reference="CS-001",
            created_by=owner_user_a.id,
        )
        assert je.id is not None
        assert je.state == "posted"

        # 3. Verify Trial Balance is balanced
        tb = get_trial_balance(db_session, tenant_a.id)
        assert tb["is_balanced"] is True
        assert tb["grand_total_debits_santim"] == 500_000
        assert tb["grand_total_credits_santim"] == 500_000

    def test_unbalanced_journal_entry_raises_error(
        self, db_session: Session, tenant_a: Tenant, owner_user_a: User
    ):
        set_tenant(db_session, tenant_a.id)
        accounts = init_default_chart_of_accounts(db_session, tenant_a.id)
        acc_dict = {a.account_code: a for a in accounts}

        # Attempt to post unbalanced entry: Dr 500,000 vs Cr 400,000
        with pytest.raises(DoubleEntryImbalanceError) as exc:
            post_journal_entry(
                session=db_session,
                tenant_id=tenant_a.id,
                entry_number="JE-UNBALANCED",
                entry_date=date.today(),
                description="Unbalanced entry test",
                lines_data=[
                    (acc_dict["1010"].id, 500_000, 0, "Debit"),
                    (acc_dict["4000"].id, 0, 400_000, "Credit"),
                ],
            )
        assert "out of balance" in str(exc.value)

    def test_pnl_and_peachtree_export(
        self, db_session: Session, tenant_a: Tenant, owner_user_a: User
    ):
        set_tenant(db_session, tenant_a.id)
        accounts = init_default_chart_of_accounts(db_session, tenant_a.id)
        acc_dict = {a.account_code: a for a in accounts}

        today = date.today()

        # Sale: Dr AR 1200, Cr Revenue 4000 (10,000 ETB = 1,000,000 santim)
        post_journal_entry(
            db_session, tenant_a.id, "JE-SALE-1", today, "Credit Sale",
            [
                (acc_dict["1200"].id, 1_000_000, 0, "AR"),
                (acc_dict["4000"].id, 0, 1_000_000, "Revenue"),
            ]
        )

        # COGS: Dr COGS 5000, Cr Inventory 1300 (7,000 ETB = 700,000 santim)
        post_journal_entry(
            db_session, tenant_a.id, "JE-COGS-1", today, "COGS Recognition",
            [
                (acc_dict["5000"].id, 700_000, 0, "COGS"),
                (acc_dict["1300"].id, 0, 700_000, "Inventory Reduction"),
            ]
        )

        # Profit & Loss
        pnl = get_profit_and_loss(db_session, tenant_a.id, today, today)
        assert pnl["revenue_santim"] == 1_000_000
        assert pnl["cogs_santim"] == 700_000
        assert pnl["gross_profit_santim"] == 300_000  # 3,000.00 ETB profit
        assert pnl["net_income_santim"] == 300_000

        # Peachtree Export
        csv_out = export_peachtree_journal_csv(db_session, tenant_a.id, today, today)
        assert "Date,Reference,Description,GL Account" in csv_out
        assert "10000.00" in csv_out
        assert "4000" in csv_out
        assert "5000" in csv_out
