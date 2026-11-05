"""
app/reports/health_check_html.py

Self-contained HTML report renderer for Collections Health Check.
Zero external CSS, zero external fonts, zero external JS/CDN dependencies.
100% offline and print/PDF-ready.
Supports bilingual rendering: English ('en') and Amharic ('am').
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Sequence

from app.kernel.money import to_etb


I18N = {
    "en": {
        "confidential": "CONFIDENTIAL",
        "confidential_notice": "This document contains proprietary financial diagnostics prepared for the business owner. Do not distribute without authorization.",
        "title": "Collections Health Check Report",
        "subtitle": "Accounts Receivable & Bank Statement Reconciliation Diagnostic",
        "meta_date": "Analysis Date",
        "meta_tenant": "Account / Business",
        "meta_invoices": "Invoices Analyzed",
        "meta_statements": "Statement Lines",
        "meta_files": "Source Files",
        "fix_first_title": "Fix These First — Prioritized Action Items",
        "fix_first_subtitle": "Immediate revenue recovery and cash leak prevention steps ranked by urgency.",
        "aging_title": "1. Receivables Aging Distribution",
        "aging_subtitle": "Open customer receivables grouped by days overdue. Only confirmed payments deduct from balances.",
        "bucket_current": "Current (Not Due)",
        "bucket_1_30": "1–30 Days Overdue",
        "bucket_31_60": "31–60 Days Overdue",
        "bucket_61_90": "61–90 Days Overdue",
        "bucket_90_plus": "90+ Days Overdue",
        "total_outstanding": "Total Outstanding",
        "total_overdue": "Total Overdue",
        "col_bucket": "Aging Bucket",
        "col_invoices_count": "Invoice Count",
        "col_original": "Original Total",
        "col_paid": "Confirmed Paid",
        "col_remaining": "Outstanding Balance",
        "col_pct_debt": "% of Total Debt",
        "top_debtors_title": "2. Top 10 Overdue Retailers",
        "top_debtors_subtitle": "Concentration of outstanding debt across your largest overdue accounts.",
        "col_rank": "#",
        "col_retailer": "Retailer Name",
        "col_phone": "Phone Number",
        "col_overdue_amt": "Overdue Amount",
        "col_dso": "Avg Days to Collect",
        "col_pct_overdue": "% of Overdue",
        "col_status": "Status",
        "matching_title": "3. Statement Reconciliation Breakdown",
        "matching_subtitle": "Categorization of bank and mobile money inflows against active invoices.",
        "match_auto": "Auto-Confirmed (Tier A)",
        "match_queued": "Queued for Review (Tier B / C / Partial)",
        "match_unmatched": "Unmatched Inflows",
        "col_category": "Reconciliation Status",
        "col_line_count": "Transactions",
        "col_total_amount": "Total Amount",
        "col_evidence": "Evidence Level",
        "col_notes": "Diagnostic Notes",
        "discrepancies_title": "4. Cash Leak & Discrepancy Findings",
        "discrepancies_subtitle": "Anomalies detected in sales sequencing, overpayments, and unlinked bank deposits.",
        "sub_duplicates": "Possible Duplicate Payments & Overpayments",
        "sub_gaps": "Invoice Sequence Gaps (Missing Sales Slips)",
        "sub_unmatched": "Unidentified Bank Deposits (No Invoice Found)",
        "col_inv_num": "Invoice #",
        "col_payment_ref": "Payment Ref / Narration",
        "col_date": "Value Date",
        "col_amount": "Amount",
        "col_excess": "Excess / Unallocated",
        "col_reason": "Detection Reason",
        "col_gap": "Missing Invoice Range",
        "uncertain_badge": "Uncertain",
        "no_data": "None detected in imported records.",
        "footer_prepared": "Prepared by Birana Technology",
        "footer_date": "Generated on",
        "footer_confidential": "Birana Technology · Daily Close · All rights reserved · Addis Ababa, Ethiopia",
        "days_unit": "days",
        "days_na": "N/A",
    },
    "am": {
        "confidential": "ሚስጥራዊ",
        "confidential_notice": "ይህ ሰነድ ለንግድ ድርጅቱ ባለቤት የተዘጋጀ የፋይናንስ ምርመራ ነው። ያለፈቃድ ማሰራጨት በጥብቅ የተከለከለ ነው።",
        "title": "የብድርና ሂሳብ አሰባሰብ ምርመራ ሪፖርት",
        "subtitle": "ያልተሰበሰቡ የብድር ሽያጮች እና የባንክ ገቢዎች ማስታረቂያ ምርመራ",
        "meta_date": "የተመረመረበት ቀን",
        "meta_tenant": "የድርጅቱ ስም",
        "meta_invoices": "የተመረመሩ ደረሰኞች",
        "meta_statements": "የባንክ እንቅስቃሴዎች",
        "meta_files": "የተመረመሩ ፋይሎች",
        "fix_first_title": "ቅድሚያ የሚሰጣቸው እርምጃዎች",
        "fix_first_subtitle": "ገንዘብ ለማስመለስና የገንዘብ መፍሰስን ለመከላከል በአስቸኳይ መከናወን ያለባቸው ተግባራት።",
        "aging_title": "1. የደንበኞች የብድር ዕድሜ ክፍፍል",
        "aging_subtitle": "ደንበኞች ያልከፈሉት የብድር መጠን እንደቆየበት ጊዜ። የተረጋገጡ ክፍያዎች ብቻ ቀሪ ሂሳብን ይቀንሳሉ።",
        "bucket_current": "የወቅቱ (ያልደረሰ)",
        "bucket_1_30": "ከ1–30 ቀናት ያለፈበት",
        "bucket_31_60": "ከ31–60 ቀናት ያለፈበት",
        "bucket_61_90": "ከ61–90 ቀናት ያለፈበት",
        "bucket_90_plus": "ከ90 ቀናት በላይ ያለፈበት",
        "total_outstanding": "አጠቃላይ ያልተሰበሰበ",
        "total_overdue": "ጊዜው ያለፈበት ብድር",
        "col_bucket": "የብድር ዕድሜ",
        "col_invoices_count": "የደረሰኝ ብዛት",
        "col_original": "ዋናው መጠን",
        "col_paid": "የተረጋገጠ የተከፈለ",
        "col_remaining": "ቀሪ ያልተከፈለ",
        "col_pct_debt": "ከአጠቃላይ ዕዳ %",
        "top_debtors_title": "2. ከፍተኛ ዕዳ ያለባቸው 10 ደንበኞች",
        "top_debtors_subtitle": "ያልተሰበሰበው ብድር በከፍተኛ ደረጃ የተከማቸባቸው ቸርቻሪዎች ዝርዝር።",
        "col_rank": "ተ.ቁ",
        "col_retailer": "የደንበኛው ስም",
        "col_phone": "ስልክ ቁጥር",
        "col_overdue_amt": "ጊዜው ያለፈበት ዕዳ",
        "col_dso": "አማካይ የመሰብሰቢያ ጊዜ",
        "col_pct_overdue": "ከጠቅላላ ያለፈበት %",
        "col_status": "ሁኔታ",
        "matching_title": "3. የባንክና ሞባይል ክፍያዎች ማስታረቂያ",
        "matching_subtitle": "ወደ ባንክና ቴሌብር የገቡ ክፍያዎች ከደረሰኞች ጋር የተጣመሩበት ሁኔታ።",
        "match_auto": "በራስ-ሰር የተረጋገጠ (Tier A)",
        "match_queued": "በግምገማ ላይ (Tier B / C / ከፊል)",
        "match_unmatched": "ያልተጣመረ ገቢ",
        "col_category": "የማስታረቂያ ሁኔታ",
        "col_line_count": "የግብይት ብዛት",
        "col_total_amount": "አጠቃላይ መጠን",
        "col_evidence": "የማስረጃ ደረጃ",
        "col_notes": "ዝርዝር ማብራሪያ",
        "discrepancies_title": "4. የገንዘብ መፍሰስና ያልተለመዱ ግኝቶች",
        "discrepancies_subtitle": "በደረሰኝ ቅደም ተከተል፣ በተደጋጋሚ ክፍያዎችና ባልታወቁ ገቢዎች ላይ የተገኙ ግድፈቶች።",
        "sub_duplicates": "ተደጋጋሚ የተከፈሉ ወይም የተረፉ ክፍያዎች",
        "sub_gaps": "ያልተመዘገቡ ወይም የጎደሉ ደረሰኞች",
        "sub_unmatched": "ባለቤታቸው ያልታወቀ የባንክ ገቢዎች (ደረሰኝ የሌላቸው)",
        "col_inv_num": "የደረሰኝ #",
        "col_payment_ref": "የክፍያ ማጣቀሻ / መግለጫ",
        "col_date": "የክፍያ ቀን",
        "col_amount": "መጠን",
        "col_excess": "ትርፍ / ያልተመደበ",
        "col_reason": "የተገኘበት ምክንያት",
        "col_gap": "የጎደሉ ደረሰኞች ቅደም ተከተል",
        "uncertain_badge": "ያልተረጋገጠ",
        "no_data": "በተመረመሩት መረጃዎች ውስጥ ምንም ችግር አልተገኘም።",
        "footer_prepared": "በቢራና ቴክኖሎጂ የተዘጋጀ",
        "footer_date": "የተዘጋጀበት ቀን",
        "footer_confidential": "ቢራና ቴክኖሎጂ · ዕለታዊ ሂሳብ ማጠቃለያ · መብቱ በሕግ የተጠበቀ · አዲስ አበባ፣ ኢትዮጵያ",
        "days_unit": "ቀናት",
        "days_na": "አልተገኘም",
    },
}


CSS_STYLES = """
:root {
  --bg-primary: #0f172a;
  --bg-secondary: #1e293b;
  --bg-card: #ffffff;
  --text-main: #1e293b;
  --text-muted: #64748b;
  --border: #e2e8f0;
  --accent: #2563eb;
  --accent-light: #eff6ff;
  --danger: #dc2626;
  --danger-light: #fef2f2;
  --warning: #d97706;
  --warning-light: #fffbeb;
  --success: #16a34a;
  --success-light: #f0fdf4;
}

* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, "Noto Sans Ethiopic", sans-serif;
  background-color: #f8fafc;
  color: var(--text-main);
  line-height: 1.5;
  padding: 24px;
}
.report-container {
  max-width: 1100px;
  margin: 0 auto;
  background: #ffffff;
  border: 1px solid var(--border);
  border-radius: 8px;
  box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05);
  overflow: hidden;
}

/* Confidential Banner */
.confidential-header {
  background: #7f1d1d;
  color: #ffffff;
  padding: 10px 24px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 13px;
  letter-spacing: 0.05em;
  font-weight: 700;
}
.confidential-notice {
  font-weight: 400;
  font-size: 11px;
  opacity: 0.9;
}

/* Report Header */
.report-header {
  padding: 32px 36px 24px 36px;
  border-bottom: 2px solid var(--border);
  background: #fbfcfd;
}
.brand-title {
  font-size: 13px;
  text-transform: uppercase;
  color: var(--accent);
  font-weight: 800;
  letter-spacing: 0.1em;
  margin-bottom: 6px;
}
.report-title {
  font-size: 26px;
  font-weight: 800;
  color: #0f172a;
  margin-bottom: 4px;
}
.report-subtitle {
  font-size: 14px;
  color: var(--text-muted);
  margin-bottom: 20px;
}
.meta-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 16px;
  padding-top: 16px;
  border-top: 1px dashed var(--border);
}
.meta-item {
  font-size: 12px;
}
.meta-label {
  color: var(--text-muted);
  text-transform: uppercase;
  font-weight: 600;
  font-size: 11px;
}
.meta-val {
  font-size: 14px;
  font-weight: 700;
  color: #0f172a;
  margin-top: 2px;
}

/* Action Box */
.section {
  padding: 28px 36px;
  border-bottom: 1px solid var(--border);
}
.section-title {
  font-size: 18px;
  font-weight: 700;
  color: #0f172a;
  margin-bottom: 4px;
}
.section-subtitle {
  font-size: 13px;
  color: var(--text-muted);
  margin-bottom: 18px;
}

.action-box {
  background: #fffbeb;
  border: 1px solid #fde68a;
  border-left: 5px solid #d97706;
  border-radius: 6px;
  padding: 20px 24px;
}
.action-box h3 {
  font-size: 16px;
  color: #92400e;
  margin-bottom: 12px;
  font-weight: 700;
}
.action-list {
  list-style: none;
}
.action-item {
  position: relative;
  padding-left: 28px;
  margin-bottom: 10px;
  font-size: 13.5px;
  color: #78350f;
  line-height: 1.45;
}
.action-item::before {
  content: "→";
  position: absolute;
  left: 6px;
  top: 0;
  font-weight: 800;
  color: #b45309;
}

/* Aging Card Grid */
.cards-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 14px;
  margin-bottom: 22px;
}
.metric-card {
  padding: 16px;
  border-radius: 6px;
  border: 1px solid var(--border);
  background: #f8fafc;
}
.card-danger {
  background: var(--danger-light);
  border-color: #fecaca;
}
.card-warning {
  background: var(--warning-light);
  border-color: #fde68a;
}
.card-success {
  background: var(--success-light);
  border-color: #bbf7d0;
}
.metric-card .card-title {
  font-size: 11.5px;
  font-weight: 600;
  color: var(--text-muted);
  text-transform: uppercase;
}
.metric-card .card-value {
  font-size: 19px;
  font-weight: 800;
  color: #0f172a;
  margin-top: 6px;
}
.metric-card .card-meta {
  font-size: 11.5px;
  color: var(--text-muted);
  margin-top: 4px;
}

/* Tables */
.table-wrap {
  width: 100%;
  overflow-x: auto;
  border: 1px solid var(--border);
  border-radius: 6px;
}
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
  text-align: left;
}
thead th {
  background: #f1f5f9;
  padding: 10px 14px;
  font-size: 11.5px;
  font-weight: 700;
  text-transform: uppercase;
  color: #475569;
  border-bottom: 1px solid var(--border);
}
tbody td {
  padding: 10px 14px;
  border-bottom: 1px solid #f1f5f9;
}
tbody tr:last-child td {
  border-bottom: none;
}
tbody tr:hover {
  background: #f8fafc;
}
tfoot td {
  padding: 12px 14px;
  font-weight: 800;
  background: #f8fafc;
  border-top: 2px solid var(--border);
}
.text-right { text-align: right; }
.text-center { text-align: center; }

/* Badges */
.badge {
  display: inline-block;
  padding: 2px 7px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.badge-danger { background: #fee2e2; color: #991b1b; }
.badge-warning { background: #fef3c7; color: #92400e; }
.badge-success { background: #dcfce7; color: #166534; }
.badge-info { background: #e0f2fe; color: #075985; }
.badge-neutral { background: #f1f5f9; color: #475569; }

/* Footer */
.report-footer {
  padding: 24px 36px;
  background: #f8fafc;
  border-top: 1px solid var(--border);
  font-size: 12px;
  color: var(--text-muted);
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.footer-logo {
  font-weight: 700;
  color: #0f172a;
}

/* Print Rules */
@media print {
  body { padding: 0; background: #ffffff; }
  .report-container { border: none; box-shadow: none; max-width: 100%; }
  .page-break { page-break-before: always; }
  .confidential-header { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  .action-box, .metric-card, .badge { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
}
"""


def render_collections_health_check_html(
    report_data: dict[str, Any],
    *,
    lang: str = "en",
) -> str:
    """
    Render standalone, offline-ready HTML document for Collections Health Check.
    """
    tr = I18N.get(lang, I18N["en"])
    meta = report_data.get("meta", {})
    aging = report_data.get("aging", {})
    debtors = report_data.get("top_debtors", [])
    matching = report_data.get("matching", {})
    discrepancies = report_data.get("discrepancies", {})
    action_items = report_data.get("action_items", [])

    # Format aging cards
    buckets_data = aging.get("buckets", [])
    bucket_map = {b["key"]: b for b in buckets_data}

    curr_b = bucket_map.get("current", {"remaining_etb": "0.00 ETB", "count": 0})
    b_1_30 = bucket_map.get("1_30", {"remaining_etb": "0.00 ETB", "count": 0})
    b_31_60 = bucket_map.get("31_60", {"remaining_etb": "0.00 ETB", "count": 0})
    b_61_90 = bucket_map.get("61_90", {"remaining_etb": "0.00 ETB", "count": 0})
    b_90_plus = bucket_map.get("90_plus", {"remaining_etb": "0.00 ETB", "count": 0})

    # Render Action Items
    action_html = ""
    for item in action_items:
        action_html += f'<li class="action-item"><strong>{html.escape(item.get("title", ""))}</strong>: {html.escape(item.get("desc", ""))}</li>\n'
    if not action_items:
        action_html = f'<li class="action-item">{html.escape(tr["no_data"])}</li>'

    # Render Aging Table Rows
    aging_rows = ""
    for b in buckets_data:
        key_label = tr.get(f"bucket_{b['key']}", b.get("label", b["key"]))
        badge_cls = "badge-neutral"
        if b["key"] == "current":
            badge_cls = "badge-success"
        elif b["key"] in ("1_30", "31_60"):
            badge_cls = "badge-warning"
        elif b["key"] in ("61_90", "90_plus"):
            badge_cls = "badge-danger"

        aging_rows += f"""
        <tr>
          <td><span class="badge {badge_cls}">{html.escape(key_label)}</span></td>
          <td class="text-center">{b.get('count', 0)}</td>
          <td class="text-right">{html.escape(b.get('original_etb', '0.00 ETB'))}</td>
          <td class="text-right">{html.escape(b.get('paid_etb', '0.00 ETB'))}</td>
          <td class="text-right"><strong>{html.escape(b.get('remaining_etb', '0.00 ETB'))}</strong></td>
          <td class="text-right">{b.get('pct_debt', 0.0):.1f}%</td>
        </tr>
        """

    # Render Top 10 Debtors Rows
    debtors_rows = ""
    for idx, d in enumerate(debtors, start=1):
        phone_display = html.escape(d.get("phone") or "—")
        dso_val = d.get("avg_days_to_collect")
        dso_display = f"{dso_val:.1f} {tr['days_unit']}" if dso_val is not None else tr["days_na"]
        debtors_rows += f"""
        <tr>
          <td class="text-center">{idx}</td>
          <td><strong>{html.escape(d.get('name', 'Unknown'))}</strong></td>
          <td>{phone_display}</td>
          <td class="text-right"><strong style="color: var(--danger);">{html.escape(d.get('overdue_etb', '0.00 ETB'))}</strong></td>
          <td class="text-center">{html.escape(dso_display)}</td>
          <td class="text-right">{d.get('pct_of_overdue', 0.0):.1f}%</td>
        </tr>
        """
    if not debtors:
        debtors_rows = f'<tr><td colspan="6" class="text-center" style="padding: 20px; color: var(--text-muted);">{html.escape(tr["no_data"])}</td></tr>'

    # Render Matching Table Rows
    matching_categories = [
        ("match_auto", matching.get("auto_confirmed", {}), "badge-success", "L1 / L2"),
        ("match_queued", matching.get("queued", {}), "badge-warning", "L2 / L3 / L4"),
        ("match_unmatched", matching.get("unmatched", {}), "badge-danger", "L2"),
    ]
    matching_rows = ""
    for cat_key, cat_data, badge_cls, ev_level in matching_categories:
        cat_label = tr[cat_key]
        matching_rows += f"""
        <tr>
          <td><span class="badge {badge_cls}">{html.escape(cat_label)}</span></td>
          <td class="text-center">{cat_data.get('count', 0)}</td>
          <td class="text-right"><strong>{html.escape(cat_data.get('amount_etb', '0.00 ETB'))}</strong></td>
          <td class="text-center"><span class="badge badge-neutral">{html.escape(ev_level)}</span></td>
          <td>{html.escape(cat_data.get('notes', ''))}</td>
        </tr>
        """

    # Render Duplicates & Overpayments Rows
    dups = discrepancies.get("duplicates_and_overpayments", [])
    dups_rows = ""
    for item in dups:
        dups_rows += f"""
        <tr>
          <td><strong>{html.escape(item.get('invoice_number', '—'))}</strong></td>
          <td>{html.escape(item.get('payment_ref', '—'))}</td>
          <td class="text-center">{html.escape(str(item.get('date', '—')))}</td>
          <td class="text-right">{html.escape(item.get('amount_etb', '0.00 ETB'))}</td>
          <td class="text-right" style="color: var(--danger);"><strong>{html.escape(item.get('excess_etb', '—'))}</strong></td>
          <td><span class="badge badge-warning">[{html.escape(tr['uncertain_badge'])}]</span> {html.escape(item.get('reason', ''))}</td>
        </tr>
        """
    if not dups:
        dups_rows = f'<tr><td colspan="6" class="text-center" style="padding: 16px; color: var(--text-muted);">{html.escape(tr["no_data"])}</td></tr>'

    # Render Sequence Gaps Rows
    gaps = discrepancies.get("sequence_gaps", [])
    gaps_rows = ""
    for gap in gaps:
        gaps_rows += f"""
        <tr>
          <td><strong style="color: var(--danger);">{html.escape(gap)}</strong></td>
          <td><span class="badge badge-danger">[{html.escape(tr['uncertain_badge'])}] Missing Sales Slip Range</span></td>
        </tr>
        """
    if not gaps:
        gaps_rows = f'<tr><td colspan="2" class="text-center" style="padding: 16px; color: var(--text-muted);">{html.escape(tr["no_data"])}</td></tr>'

    # Render Unidentified Deposits
    unmatched_deposits = discrepancies.get("unidentified_deposits", [])
    unmatched_rows = ""
    for dep in unmatched_deposits[:15]:  # limit 15 for readability
        unmatched_rows += f"""
        <tr>
          <td class="text-center">{html.escape(str(dep.get('date', '—')))}</td>
          <td>{html.escape(dep.get('reference', '—'))}</td>
          <td>{html.escape(dep.get('counterparty', '—'))}</td>
          <td class="text-right"><strong>{html.escape(dep.get('amount_etb', '0.00 ETB'))}</strong></td>
          <td class="text-center"><span class="badge badge-neutral">{html.escape(dep.get('evidence_level', 'L2'))}</span></td>
          <td><span class="badge badge-warning">[{html.escape(tr['uncertain_badge'])}] Unlinked Deposit</span></td>
        </tr>
        """
    if not unmatched_deposits:
        unmatched_rows = f'<tr><td colspan="6" class="text-center" style="padding: 16px; color: var(--text-muted);">{html.escape(tr["no_data"])}</td></tr>'

    # Format Meta
    source_files_str = ", ".join(meta.get("source_files", []))
    gen_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Final Document HTML
    return f"""<!DOCTYPE html>
<html lang="{lang}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{html.escape(tr["title"])} - Daily Close</title>
  <style>
{CSS_STYLES}
  </style>
</head>
<body>
  <div class="report-container">
    
    <!-- Confidential Banner -->
    <div class="confidential-header">
      <div>🔒 {html.escape(tr["confidential"])}</div>
      <div class="confidential-notice">{html.escape(tr["confidential_notice"])}</div>
    </div>

    <!-- Header & Metadata -->
    <header class="report-header">
      <div class="brand-title">Daily Close · ዕለታዊ ሂሳብ ማጠቃለያ</div>
      <h1 class="report-title">{html.escape(tr["title"])}</h1>
      <p class="report-subtitle">{html.escape(tr["subtitle"])}</p>

      <div class="meta-grid">
        <div class="meta-item">
          <div class="meta-label">{html.escape(tr["meta_tenant"])}</div>
          <div class="meta-val">{html.escape(meta.get("tenant_name", "Enterprise"))}</div>
        </div>
        <div class="meta-item">
          <div class="meta-label">{html.escape(tr["meta_date"])}</div>
          <div class="meta-val">{html.escape(str(meta.get("as_of_date", date.today())))}</div>
        </div>
        <div class="meta-item">
          <div class="meta-label">{html.escape(tr["meta_invoices"])}</div>
          <div class="meta-val">{meta.get("total_invoices_count", 0):,}</div>
        </div>
        <div class="meta-item">
          <div class="meta-label">{html.escape(tr["meta_statements"])}</div>
          <div class="meta-val">{meta.get("total_statement_lines_count", 0):,}</div>
        </div>
        <div class="meta-item" style="grid-column: 1 / -1;">
          <div class="meta-label">{html.escape(tr["meta_files"])}</div>
          <div class="meta-val" style="font-weight: 500; font-size: 12px; color: #475569;">{html.escape(source_files_str)}</div>
        </div>
      </div>
    </header>

    <!-- Fix These First Action Plan -->
    <section class="section" style="background: #fafafa;">
      <div class="action-box">
        <h3>⚡ {html.escape(tr["fix_first_title"])}</h3>
        <p style="font-size: 13px; color: #92400e; margin-bottom: 14px;">{html.escape(tr["fix_first_subtitle"])}</p>
        <ul class="action-list">
          {action_html}
        </ul>
      </div>
    </section>

    <!-- Section 1: Receivables Aging Breakdown -->
    <section class="section">
      <h2 class="section-title">{html.escape(tr["aging_title"])}</h2>
      <p class="section-subtitle">{html.escape(tr["aging_subtitle"])}</p>

      <!-- 5-Bucket Cards Grid -->
      <div class="cards-grid">
        <div class="metric-card card-success">
          <div class="card-title">{html.escape(tr["bucket_current"])}</div>
          <div class="card-value">{html.escape(curr_b.get("remaining_etb", "0.00 ETB"))}</div>
          <div class="card-meta">{curr_b.get("count", 0)} {html.escape(tr["meta_invoices"])}</div>
        </div>
        <div class="metric-card card-warning">
          <div class="card-title">{html.escape(tr["bucket_1_30"])}</div>
          <div class="card-value">{html.escape(b_1_30.get("remaining_etb", "0.00 ETB"))}</div>
          <div class="card-meta">{b_1_30.get("count", 0)} {html.escape(tr["meta_invoices"])}</div>
        </div>
        <div class="metric-card card-warning">
          <div class="card-title">{html.escape(tr["bucket_31_60"])}</div>
          <div class="card-value">{html.escape(b_31_60.get("remaining_etb", "0.00 ETB"))}</div>
          <div class="card-meta">{b_31_60.get("count", 0)} {html.escape(tr["meta_invoices"])}</div>
        </div>
        <div class="metric-card card-danger">
          <div class="card-title">{html.escape(tr["bucket_61_90"])}</div>
          <div class="card-value">{html.escape(b_61_90.get("remaining_etb", "0.00 ETB"))}</div>
          <div class="card-meta">{b_61_90.get("count", 0)} {html.escape(tr["meta_invoices"])}</div>
        </div>
        <div class="metric-card card-danger">
          <div class="card-title">{html.escape(tr["bucket_90_plus"])}</div>
          <div class="card-value">{html.escape(b_90_plus.get("remaining_etb", "0.00 ETB"))}</div>
          <div class="card-meta">{b_90_plus.get("count", 0)} {html.escape(tr["meta_invoices"])}</div>
        </div>
      </div>

      <!-- Aging Summary Table -->
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>{html.escape(tr["col_bucket"])}</th>
              <th class="text-center">{html.escape(tr["col_invoices_count"])}</th>
              <th class="text-right">{html.escape(tr["col_original"])}</th>
              <th class="text-right">{html.escape(tr["col_paid"])}</th>
              <th class="text-right">{html.escape(tr["col_remaining"])}</th>
              <th class="text-right">{html.escape(tr["col_pct_debt"])}</th>
            </tr>
          </thead>
          <tbody>
            {aging_rows}
          </tbody>
          <tfoot>
            <tr>
              <td>{html.escape(tr["total_outstanding"])}</td>
              <td class="text-center">{aging.get("grand_total_count", 0)}</td>
              <td class="text-right">{html.escape(aging.get("grand_original_etb", "0.00 ETB"))}</td>
              <td class="text-right">{html.escape(aging.get("grand_paid_etb", "0.00 ETB"))}</td>
              <td class="text-right"><span style="color: var(--accent);">{html.escape(aging.get("grand_remaining_etb", "0.00 ETB"))}</span></td>
              <td class="text-right">100.0%</td>
            </tr>
          </tfoot>
        </table>
      </div>
    </section>

    <!-- Section 2: Top 10 Overdue Retailers -->
    <section class="section page-break">
      <h2 class="section-title">{html.escape(tr["top_debtors_title"])}</h2>
      <p class="section-subtitle">{html.escape(tr["top_debtors_subtitle"])}</p>

      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th class="text-center">{html.escape(tr["col_rank"])}</th>
              <th>{html.escape(tr["col_retailer"])}</th>
              <th>{html.escape(tr["col_phone"])}</th>
              <th class="text-right">{html.escape(tr["col_overdue_amt"])}</th>
              <th class="text-center">{html.escape(tr["col_dso"])}</th>
              <th class="text-right">{html.escape(tr["col_pct_overdue"])}</th>
            </tr>
          </thead>
          <tbody>
            {debtors_rows}
          </tbody>
        </table>
      </div>
    </section>

    <!-- Section 3: Statement Reconciliation Breakdown -->
    <section class="section">
      <h2 class="section-title">{html.escape(tr["matching_title"])}</h2>
      <p class="section-subtitle">{html.escape(tr["matching_subtitle"])}</p>

      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>{html.escape(tr["col_category"])}</th>
              <th class="text-center">{html.escape(tr["col_line_count"])}</th>
              <th class="text-right">{html.escape(tr["col_total_amount"])}</th>
              <th class="text-center">{html.escape(tr["col_evidence"])}</th>
              <th>{html.escape(tr["col_notes"])}</th>
            </tr>
          </thead>
          <tbody>
            {matching_rows}
          </tbody>
          <tfoot>
            <tr>
              <td>Total Statements Processed</td>
              <td class="text-center">{matching.get("total_lines", 0)}</td>
              <td class="text-right">{html.escape(matching.get("total_amount_etb", "0.00 ETB"))}</td>
              <td colspan="2"></td>
            </tr>
          </tfoot>
        </table>
      </div>
    </section>

    <!-- Section 4: Cash Leaks & Discrepancy Findings -->
    <section class="section page-break">
      <h2 class="section-title">{html.escape(tr["discrepancies_title"])}</h2>
      <p class="section-subtitle">{html.escape(tr["discrepancies_subtitle"])}</p>

      <!-- Sub A: Duplicates & Overpayments -->
      <h3 style="font-size: 14.5px; font-weight: 700; margin-bottom: 8px;">{html.escape(tr["sub_duplicates"])}</h3>
      <div class="table-wrap" style="margin-bottom: 24px;">
        <table>
          <thead>
            <tr>
              <th>{html.escape(tr["col_inv_num"])}</th>
              <th>{html.escape(tr["col_payment_ref"])}</th>
              <th class="text-center">{html.escape(tr["col_date"])}</th>
              <th class="text-right">{html.escape(tr["col_amount"])}</th>
              <th class="text-right">{html.escape(tr["col_excess"])}</th>
              <th>{html.escape(tr["col_reason"])}</th>
            </tr>
          </thead>
          <tbody>
            {dups_rows}
          </tbody>
        </table>
      </div>

      <!-- Sub B: Invoice Sequence Gaps -->
      <h3 style="font-size: 14.5px; font-weight: 700; margin-bottom: 8px;">{html.escape(tr["sub_gaps"])}</h3>
      <div class="table-wrap" style="margin-bottom: 24px;">
        <table>
          <thead>
            <tr>
              <th style="width: 250px;">{html.escape(tr["col_gap"])}</th>
              <th>{html.escape(tr["col_reason"])}</th>
            </tr>
          </thead>
          <tbody>
            {gaps_rows}
          </tbody>
        </table>
      </div>

      <!-- Sub C: Unidentified Deposits -->
      <h3 style="font-size: 14.5px; font-weight: 700; margin-bottom: 8px;">{html.escape(tr["sub_unmatched"])}</h3>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th class="text-center">{html.escape(tr["col_date"])}</th>
              <th>{html.escape(tr["col_payment_ref"])}</th>
              <th>Counterparty</th>
              <th class="text-right">{html.escape(tr["col_amount"])}</th>
              <th class="text-center">{html.escape(tr["col_evidence"])}</th>
              <th>{html.escape(tr["col_reason"])}</th>
            </tr>
          </thead>
          <tbody>
            {unmatched_rows}
          </tbody>
        </table>
      </div>
    </section>

    <!-- Footer -->
    <footer class="report-footer">
      <div>
        <div class="footer-logo">Birana Technology · ቢራና ቴክኖሎጂ</div>
        <div>{html.escape(tr["footer_confidential"])}</div>
      </div>
      <div class="text-right">
        <div>{html.escape(tr["footer_prepared"])}</div>
        <div style="font-size: 11px;">{html.escape(tr["footer_date"])}: {html.escape(gen_time_str)}</div>
      </div>
    </footer>

  </div>
</body>
</html>
"""
