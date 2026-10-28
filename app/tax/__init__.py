"""
app/tax/__init__.py

ERP Ethiopian Fiscal & Tax Compliance Module (MOR / ERCA).
"""

from app.tax.service import (
    calculate_tax_breakdown,
    generate_mor_vat_declaration_csv,
    issue_withholding_receipt,
)

__all__ = [
    "calculate_tax_breakdown",
    "issue_withholding_receipt",
    "generate_mor_vat_declaration_csv",
]
