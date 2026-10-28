"""
app/accounting/service.py

ERP Double-Entry General Ledger (GL) & Financial Reporting Service
Birana Technology — Daily Close ERP Suite

Invariants:
- All balances and ledger lines are stored in integer Santim (Rule 1).
- Double-entry balance rule is strictly enforced: Sum(Debits) == Sum(Credits) for every entry.
- Chart of accounts supports standard Ethiopian retail/wholesale categories.
- Exports journal batches formatted for Peachtree / Sage 50.
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import date, datetime, timezone
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.kernel.enums import AccountCategory, JournalEntryState
from app.kernel.events import append_event
from app.kernel.models import Account, JournalEntry, JournalLine
from app.reports.exports import sanitize_csv_row


class DoubleEntryImbalanceError(ValueError):
    """Raised when total debits do not equal total credits in a journal entry."""


# Standard Ethiopian Wholesale Chart of Accounts
DEFAULT_ACCOUNTS = [
    ("1010", "Cash on Hand / የካዝና ገንዘብ", AccountCategory.ASSET),
    ("1020", "Bank - CBE / የኢትዮጵያ ንግድ ባንክ", AccountCategory.ASSET),
    ("1030", "Telebirr Merchant Account / ቴሌብር", AccountCategory.ASSET),
    ("1200", "Accounts Receivable / ተሰብሳቢ ሂሳብ", AccountCategory.ASSET),
    ("1300", "Merchandise Inventory / የዕቃ ክምችት", AccountCategory.ASSET),
    ("2000", "Accounts Payable / ተከፋይ ሂሳብ", AccountCategory.LIABILITY),
    ("2100", "VAT Payable (15%) / የተጨማሪ እሴት ታክስ", AccountCategory.LIABILITY),
    ("2110", "Withholding Tax Payable (2%) / የግብር ተቀናሽ ሂሳብ", AccountCategory.LIABILITY),
    ("3000", "Owner Capital / የባለቤት ካፒታል", AccountCategory.EQUITY),
    ("4000", "Wholesale Sales Revenue / የጅምላ ሽያጭ ገቢ", AccountCategory.REVENUE),
    ("5000", "Cost of Goods Sold (COGS) / የተሸጡ ዕቃዎች ወጪ", AccountCategory.EXPENSE),
    ("6000", "Delivery & Logistics Expense / የትራንስፖርትና ማጓጓዣ ወጪ", AccountCategory.EXPENSE),
]


def init_default_chart_of_accounts(
    session: Session,
    tenant_id: uuid.UUID,
) -> list[Account]:
    """Seed standard Chart of Accounts for a tenant if not already present."""
    created = []
    for code, name, category in DEFAULT_ACCOUNTS:
        existing = session.execute(
            select(Account).where(
                Account.tenant_id == tenant_id,
                Account.account_code == code,
            )
        ).scalar_one_or_none()

        if not existing:
            acc = Account(
                tenant_id=tenant_id,
                account_code=code,
                account_name=name,
                category=category.value,
                is_active=True,
            )
            session.add(acc)
            created.append(acc)

    session.flush()
    return created


def post_journal_entry(
    session: Session,
    tenant_id: uuid.UUID,
    entry_number: str,
    entry_date: date,
    description: str,
    lines_data: list[tuple[uuid.UUID, int, int, str | None]],  # (account_id, debit_santim, credit_santim, line_desc)
    reference: str | None = None,
    created_by: uuid.UUID | None = None,
) -> JournalEntry:
    """
    Post a double-entry journal entry.
    Enforces strict mathematical balance: Sum(Debits) == Sum(Credits) > 0.
    """
    if not lines_data:
        raise ValueError("Journal entry must have at least two balanced lines.")

    total_debits = sum(d for _, d, _, _ in lines_data)
    total_credits = sum(c for _, _, c, _ in lines_data)

    if total_debits <= 0 or total_credits <= 0:
        raise ValueError("Journal entry must have positive debit and credit totals.")

    if total_debits != total_credits:
        raise DoubleEntryImbalanceError(
            f"Journal entry is out of balance! Total Debits: {total_debits} santim "
            f"({total_debits/100:.2f} ETB), Total Credits: {total_credits} santim "
            f"({total_credits/100:.2f} ETB)."
        )

    je = JournalEntry(
        tenant_id=tenant_id,
        entry_number=entry_number.strip().upper(),
        entry_date=entry_date,
        reference=reference.strip() if reference else None,
        description=description.strip(),
        state=JournalEntryState.POSTED.value,
        created_by=created_by,
    )
    session.add(je)
    session.flush()

    for acc_id, debit, credit, line_desc in lines_data:
        jl = JournalLine(
            tenant_id=tenant_id,
            journal_entry_id=je.id,
            account_id=acc_id,
            debit_santim=debit,
            credit_santim=credit,
            description=line_desc,
        )
        session.add(jl)

    session.flush()

    append_event(
        session=session,
        tenant_id=tenant_id,
        aggregate_type="journal_entry",
        aggregate_id=je.id,
        event_type="gl.journal_entry_posted",
        payload={
            "entry_number": je.entry_number,
            "total_santim": total_debits,
            "line_count": len(lines_data),
            "reference": je.reference,
        },
    )
    return je


def get_trial_balance(
    session: Session,
    tenant_id: uuid.UUID,
    as_of_date: date | None = None,
) -> dict[str, Any]:
    """
    Generate Trial Balance verifying Sum(Debits) == Sum(Credits) across all accounts.
    """
    stmt = (
        select(
            Account.id,
            Account.account_code,
            Account.account_name,
            Account.category,
            func.coalesce(func.sum(JournalLine.debit_santim), 0).label("total_debits"),
            func.coalesce(func.sum(JournalLine.credit_santim), 0).label("total_credits"),
        )
        .join(JournalLine, Account.id == JournalLine.account_id)
        .join(JournalEntry, JournalLine.journal_entry_id == JournalEntry.id)
        .where(
            Account.tenant_id == tenant_id,
            JournalEntry.state == JournalEntryState.POSTED.value,
        )
    )
    if as_of_date:
        stmt = stmt.where(JournalEntry.entry_date <= as_of_date)

    stmt = stmt.group_by(Account.id).order_by(Account.account_code)
    rows = session.execute(stmt).all()

    grand_debits = 0
    grand_credits = 0
    accounts_list = []

    for r in rows:
        deb = int(r.total_debits)
        cred = int(r.total_credits)
        grand_debits += deb
        grand_credits += cred

        accounts_list.append({
            "account_code": r.account_code,
            "account_name": r.account_name,
            "category": r.category,
            "debit_santim": deb,
            "credit_santim": cred,
            "debit_etb": f"{deb / 100:,.2f}",
            "credit_etb": f"{cred / 100:,.2f}",
        })

    is_balanced = (grand_debits == grand_credits)

    return {
        "as_of_date": (as_of_date or date.today()).isoformat(),
        "is_balanced": is_balanced,
        "grand_total_debits_santim": grand_debits,
        "grand_total_debits_etb": f"{grand_debits / 100:,.2f}",
        "grand_total_credits_santim": grand_credits,
        "grand_total_credits_etb": f"{grand_credits / 100:,.2f}",
        "accounts": accounts_list,
    }


def get_profit_and_loss(
    session: Session,
    tenant_id: uuid.UUID,
    start_date: date,
    end_date: date,
) -> dict[str, Any]:
    """
    Generate Income Statement (Profit & Loss): Revenue - COGS - Expenses = Net Profit.
    """
    tb = get_trial_balance(session, tenant_id, as_of_date=end_date)
    revenue_santim = 0
    cogs_santim = 0
    expense_santim = 0

    for acc in tb["accounts"]:
        cat = acc["category"]
        net = acc["credit_santim"] - acc["debit_santim"] if cat == "revenue" else acc["debit_santim"] - acc["credit_santim"]
        if cat == "revenue":
            revenue_santim += net
        elif cat == "expense":
            if "COGS" in acc["account_name"] or "5000" in acc["account_code"]:
                cogs_santim += net
            else:
                expense_santim += net

    gross_profit_santim = revenue_santim - cogs_santim
    net_income_santim = gross_profit_santim - expense_santim

    return {
        "period": f"{start_date.isoformat()} to {end_date.isoformat()}",
        "revenue_santim": revenue_santim,
        "revenue_etb": f"{revenue_santim / 100:,.2f}",
        "cogs_santim": cogs_santim,
        "cogs_etb": f"{cogs_santim / 100:,.2f}",
        "gross_profit_santim": gross_profit_santim,
        "gross_profit_etb": f"{gross_profit_santim / 100:,.2f}",
        "expenses_santim": expense_santim,
        "expenses_etb": f"{expense_santim / 100:,.2f}",
        "net_income_santim": net_income_santim,
        "net_income_etb": f"{net_income_santim / 100:,.2f}",
    }


def get_balance_sheet(
    session: Session,
    tenant_id: uuid.UUID,
    as_of_date: date | None = None,
) -> dict[str, Any]:
    """
    Generate Balance Sheet: Assets = Liabilities + Equity.
    """
    ref_date = as_of_date or date.today()
    tb = get_trial_balance(session, tenant_id, as_of_date=ref_date)

    assets_santim = 0
    liabilities_santim = 0
    equity_santim = 0

    for acc in tb["accounts"]:
        cat = acc["category"]
        if cat == "asset":
            assets_santim += (acc["debit_santim"] - acc["credit_santim"])
        elif cat == "liability":
            liabilities_santim += (acc["credit_santim"] - acc["debit_santim"])
        elif cat == "equity":
            equity_santim += (acc["credit_santim"] - acc["debit_santim"])

    pnl = get_profit_and_loss(session, tenant_id, date(ref_date.year, 1, 1), ref_date)
    retained_earnings = pnl["net_income_santim"]
    total_equity_santim = equity_santim + retained_earnings

    return {
        "as_of_date": ref_date.isoformat(),
        "total_assets_santim": assets_santim,
        "total_assets_etb": f"{assets_santim / 100:,.2f}",
        "total_liabilities_santim": liabilities_santim,
        "total_liabilities_etb": f"{liabilities_santim / 100:,.2f}",
        "total_equity_santim": total_equity_santim,
        "total_equity_etb": f"{total_equity_santim / 100:,.2f}",
        "is_balanced": (assets_santim == liabilities_santim + total_equity_santim),
    }


def export_peachtree_journal_csv(
    session: Session,
    tenant_id: uuid.UUID,
    start_date: date,
    end_date: date,
) -> str:
    """
    Export General Journal entries in Peachtree / Sage 50 CSV format.
    Peachtree Standard General Journal Columns:
    - Date
    - Reference
    - Description
    - GL Account
    - Amount (Debit positive, Credit negative)
    """
    stmt = (
        select(JournalEntry, JournalLine, Account)
        .join(JournalLine, JournalEntry.id == JournalLine.journal_entry_id)
        .join(Account, JournalLine.account_id == Account.id)
        .where(
            JournalEntry.tenant_id == tenant_id,
            JournalEntry.entry_date >= start_date,
            JournalEntry.entry_date <= end_date,
            JournalEntry.state == JournalEntryState.POSTED.value,
        )
        .order_by(JournalEntry.entry_date, JournalEntry.entry_number)
    )

    rows = session.execute(stmt).all()

    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow([
        "Date",
        "Reference",
        "Description",
        "GL Account",
        "Account Description",
        "Amount (ETB)",
    ])

    for je, jl, acc in rows:
        amount_etb = (jl.debit_santim - jl.credit_santim) / 100
        writer.writerow(sanitize_csv_row([
            je.entry_date.strftime("%m/%d/%Y"),
            je.reference or je.entry_number,
            jl.description or je.description,
            acc.account_code,
            acc.account_name,
            f"{amount_etb:.2f}",
        ]))

    return output.getvalue()
