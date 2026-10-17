"""ONE ERP financial statement presentation: print-friendly, safe Arabic/English HTML.

Accepts a preauthorized BalanceSheet DTO only. No SQL, authentication,
legal e-invoicing issuance, or live data loading occurs here.
"""
from __future__ import annotations

from decimal import Decimal
from html import escape

from .accounting import AccountingError
from .statement_details import BalanceSheet, StatementAccount

_LABELS = {
    "ar": {
        "report": "قائمة المركز المالي",
        "asof": "حتى تاريخ",
        "assets": "الأصول",
        "liabilities": "الالتزامات",
        "equity": "حقوق الملكية",
        "net_profit": "الأرباح غير المقفلة",
        "total_assets": "إجمالي الأصول",
        "total_liabilities": "إجمالي الالتزامات",
        "total_equity": "إجمالي حقوق الملكية شامل الأرباح",
        "difference": "فرق المعادلة المحاسبية",
        "balanced": "متوازن",
        "unbalanced": "غير متوازن",
        "no_activity": "لا توجد أرصدة",
        "source_count": "عدد القيود",
        "footer": "ONE ERP | تقرير تجريبي غير معتمد",
    },
    "en": {
        "report": "Statement of Financial Position",
        "asof": "As of",
        "assets": "Assets",
        "liabilities": "Liabilities",
        "equity": "Equity",
        "net_profit": "Unclosed earnings",
        "total_assets": "Total assets",
        "total_liabilities": "Total liabilities",
        "total_equity": "Total equity including earnings",
        "difference": "Accounting equation difference",
        "balanced": "Balanced",
        "unbalanced": "Out of balance",
        "no_activity": "No account balances",
        "source_count": "Source journals",
        "footer": "ONE ERP | Experimental and unaudited report",
    },
}


def _amount(value: Decimal, *, minor_units: int) -> str:
    if minor_units not in (0, 2, 3):
        raise AccountingError("Unsupported monetary presentation precision")
    if not isinstance(value, Decimal) or not value.is_finite():
        raise AccountingError("Cannot display an invalid financial amount")
    return f"{value:,.{minor_units}f}"


def _item_row(label: str, value: Decimal, *, minor_units: int,
              code: str = "", sources: int | None = None, total: bool = False) -> str:
    left = escape(label)
    if code:
        left = '<span class="account-code" dir="ltr">' + escape(code) + '</span> ' + left
    if sources is not None:
        left += ' <small class="source-count">(' + str(sources) + ')</small>'
    cls = ' class="total-row"' if total else ''
    return ('<tr' + cls + '><td>' + left + '</td><td dir="ltr" class="amount">'
            + _amount(value, minor_units=minor_units) + '</td></tr>')


def render_balance_sheet_html(
    statement: BalanceSheet, *, company_name: str,
    locale: str = "ar", minor_units: int = 2,
) -> str:
    """Output a standalone responsive/printable HTML snapshot.

    The caller must verify source authorization and tenant identity first.
    This is not audited financial advice or a government invoice.
    """
    if locale not in _LABELS:
        raise AccountingError("Report locale must be ar or en")
    if not company_name.strip():
        raise AccountingError("Company name is required")
    # Reject tampered DTOs before rendering a reassuring green status.
    difference = (statement.total_assets - statement.total_liabilities
                  - statement.total_equity_including_profit)
    if difference != statement.accounting_equation_difference:
        raise AccountingError("Reported accounting equation difference is inconsistent")
    if statement.balanced != (difference == 0):
        raise AccountingError("Reported accounting equation status is inconsistent")

    c = _LABELS[locale]
    direction = "rtl" if locale == "ar" else "ltr"

    def block(heading: str, rows: tuple[StatementAccount, ...],
              total: Decimal, total_label: str) -> str:
        inner = ''.join(
            _item_row(row.name, row.amount, code=row.code,
                      sources=len(row.source_entry_ids),
                      minor_units=minor_units)
            for row in rows
        )
        if not inner:
            inner = '<tr><td colspan="2">' + escape(c["no_activity"]) + '</td></tr>'
        inner += _item_row(total_label,total,minor_units=minor_units,total=True)
        return '<section class="financial-section"><h2>' + escape(heading) + '</h2><table><tbody>' + inner + '</tbody></table></section>'

    asset_html = block(c["assets"],statement.assets,statement.total_assets,c["total_assets"])
    liability_html = block(c["liabilities"],statement.liabilities,
                           statement.total_liabilities,c["total_liabilities"])
    equity_html = block(c["equity"],statement.equity,
                        statement.contributed_and_retained_equity,c["equity"])
    earnings_html = _item_row(c["net_profit"],statement.unclosed_profit_to_date,
                              minor_units=minor_units)
    equity_total_html = _item_row(c["total_equity"],statement.total_equity_including_profit,
                                  minor_units=minor_units,total=True)
    difference_html = _item_row(c["difference"],difference,minor_units=minor_units,total=True)
    indicator = c["balanced"] if statement.balanced else c["unbalanced"]
    indicator_style = "pass" if statement.balanced else "fail"

    return f'''<!doctype html>
<html lang="{locale}" dir="{direction}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow,noarchive">
<title>ONE ERP | {escape(c["report"])}</title>
<style>
:root {{font-family:Tahoma,Arial,sans-serif;color:#1b3854;background:#edf4f9}}
* {{box-sizing:border-box}}
body {{margin:0;padding:25px}}
.report {{max-width:1000px;margin:auto;background:#fff;border-radius:16px;
 border:1px solid #d9e5ef;box-shadow:0 12px 38px #19375213;overflow:hidden}}
header {{padding:30px 34px;background:#124f80;color:#fff}}
.brand {{font-size:24px;font-weight:800;letter-spacing:.035em}}
header h1 {{font-size:22px;margin:16px 0 6px}}
header p {{margin:0;font-size:14px}}
.meta {{padding:18px 34px;background:#f6fafe;display:flex;justify-content:space-between;gap:18px;flex-wrap:wrap}}
main {{padding:12px 34px 32px}}
.financial-section {{break-inside:avoid;margin:25px 0}}
h2 {{color:#144d7b;margin-bottom:12px;font-size:18px;border-bottom:2px solid #d6e6f2;padding-bottom:9px}}
table {{width:100%;border-collapse:collapse;font-size:14px}}
td {{padding:11px 9px;border-bottom:1px solid #e1eaf1}}
td.amount {{font-weight:650;text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
.account-code {{font-size:12px;color:#638099;padding-inline-end:10px}}
.source-count {{color:#65839d;font-size:11px}}
.total-row td {{font-weight:800;background:#eaf3fa;border-bottom:2px solid #bdd5e9}}
.status {{display:inline-block;padding:5px 12px;border-radius:30px;font-weight:700}}
.status.pass {{background:#dbeee4;color:#146244}}
.status.fail {{background:#ffe8e6;color:#a1261b}}
footer {{padding:18px 34px;text-align:center;background:#f4f8fb;color:#57758c;font-size:12px}}
@media (max-width:600px) {{body {{padding:0}} .report {{border-radius:0}} main,header,.meta {{padding-inline:17px}}}}
@media print {{
 @page {{size:A4;margin:15mm}}
 body {{background:#fff;padding:0}}
 .report {{border:0;box-shadow:none;max-width:none}}
 header {{print-color-adjust:exact;-webkit-print-color-adjust:exact}}
 .financial-section {{break-inside:avoid}}
}}
</style>
</head>
<body><div class="report">
<header><div class="brand">ONE ERP</div><h1>{escape(c["report"])}</h1>
<p>{escape(company_name)}</p></header>
<div class="meta"><span>{escape(c["asof"])}: {statement.as_of.isoformat()}</span>
<span>{escape(statement.currency)} | {escape(c["source_count"])}: {len(statement.source_entry_ids)}</span>
<span class="status {indicator_style}">{escape(indicator)}</span></div>
<main>{asset_html}{liability_html}{equity_html}
<section class="financial-section"><table><tbody>{earnings_html}{equity_total_html}{difference_html}</tbody></table></section>
</main><footer>{escape(c["footer"])}</footer>
</div></body></html>'''
