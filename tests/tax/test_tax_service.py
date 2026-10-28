"""
tests/tax/test_tax_service.py

Test Suite for ERP Ethiopian Fiscal & Tax Compliance Module (MOR / ERCA).
Verifies:
- 15% VAT and 10% TOT calculations
- 2% Withholding Tax threshold verification (10,000 ETB = 1,000,000 santim)
- Withholding receipt issuance
- Monthly Ministry of Revenues (MOR) VAT declaration export
"""

from __future__ import annotations

from datetime import date
import pytest
from sqlalchemy.orm import Session

from app.kernel.enums import TaxRateType
from app.kernel.models import Invoice, Retailer, Tenant, User
from app.kernel.tenant import set_tenant
from app.tax.service import (
    WITHHOLDING_THRESHOLD_SANTIM,
    calculate_tax_breakdown,
    generate_mor_vat_declaration_csv,
    issue_withholding_receipt,
)


@pytest.fixture(autouse=True)
def ensure_tables(tables):
    pass


class TestTaxService:
    def test_vat_and_withholding_calculation(self):
        # 10,000 ETB (1,000,000 santim)
        res = calculate_tax_breakdown(
            base_amount_santim=1_000_000,
            tax_rate_type=TaxRateType.VAT_15,
            apply_withholding=True,
        )
        # VAT 15% of 10,000 = 1,500 ETB (150,000 santim)
        assert res["vat_santim"] == 150_000
        assert res["vat_etb"] == "1,500.00"
        # Gross with VAT = 11,500 ETB (1,150,000 santim)
        assert res["gross_with_tax_santim"] == 1_150_000
        # 2% Withholding on base 10,000 ETB = 200 ETB (20,000 santim)
        assert res["withholding_santim"] == 20_000
        assert res["withholding_etb"] == "200.00"
        # Net payable = 1,150,000 - 20,000 = 1,130,000 santim (11,300.00 ETB)
        assert res["net_payable_santim"] == 1_130_000

    def test_withholding_receipt_threshold_guard(
        self, db_session: Session, tenant_a: Tenant, owner_user_a: User
    ):
        set_tenant(db_session, tenant_a.id)

        # Attempt to issue withholding for 5,000 ETB (below 10,000 ETB threshold)
        with pytest.raises(ValueError) as exc:
            issue_withholding_receipt(
                session=db_session,
                tenant_id=tenant_a.id,
                receipt_number="WHT-FAIL-01",
                counterparty_type="supplier",
                counterparty_name="Small Kiosk",
                counterparty_tin="0011223344",
                source_invoice_ref="INV-001",
                gross_amount_santim=500_000,  # 5,000 ETB
            )
        assert "below the Ethiopian withholding threshold" in str(exc.value)

        # Issue valid withholding for 50,000 ETB (5,000,000 santim)
        receipt = issue_withholding_receipt(
            session=db_session,
            tenant_id=tenant_a.id,
            receipt_number="WHT-PASS-01",
            counterparty_type="supplier",
            counterparty_name="Wonji Sugar Factory",
            counterparty_tin="0099887766",
            source_invoice_ref="INV-WONJI-10",
            gross_amount_santim=5_000_000,
            created_by=owner_user_a.id,
        )
        assert receipt.id is not None
        assert receipt.withheld_amount_santim == 100_000  # 1,000.00 ETB (2% of 50,000)

    def test_mor_monthly_vat_declaration_export(
        self, db_session: Session, tenant_a: Tenant, owner_user_a: User
    ):
        set_tenant(db_session, tenant_a.id)

        ret = Retailer(tenant_id=tenant_a.id, name="Bole Fresh Supermarket")
        db_session.add(ret)
        db_session.flush()

        inv = Invoice(
            tenant_id=tenant_a.id,
            retailer_id=ret.id,
            invoice_number="INV-VAT-01",
            amount_santim=1_000_000,  # 10,000 ETB taxable
            issued_date=date(2026, 10, 5),
            due_date=date(2026, 10, 20),
            created_by=owner_user_a.id,
        )
        db_session.add(inv)
        db_session.flush()

        csv_content = generate_mor_vat_declaration_csv(
            session=db_session,
            tenant_id=tenant_a.id,
            start_date=date(2026, 10, 1),
            end_date=date(2026, 10, 31),
        )

        assert "MINISTRY OF REVENUES (MOR)" in csv_content
        assert "INV-VAT-01" in csv_content
        assert "10000.00" in csv_content
        assert "1500.00" in csv_content
