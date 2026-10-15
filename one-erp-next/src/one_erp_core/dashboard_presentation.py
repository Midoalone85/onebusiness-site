"""Standalone private financial dashboard visualization (Arabic/English).

HTML is presentation-only. No customer-facing endpoint or production deployment
is implemented. The authenticated backend must set Cache-Control: no-store,
Content-Security-Policy and tenant-scoped access checks for every response.
"""
from __future__ import annotations

from decimal import Decimal
from html import escape

from .accounting import AccountingError
from .finance_dashboard import FinanceDashboard


_TRANSLATIONS = {
    "ar": {
        "title":"لوحة الأداء المالي",
        "company":"الشركة",
        "asof":"حتى تاريخ",
        "period":"الفترة",
        "assets":"الأصول",
        "liabilities":"الالتزامات",
        "equity":"حقوق الملكية شامل الأرباح",
        "profit":"صافي ربح الفترة",
        "revenue":"الإيرادات",
        "expense":"المصروفات",
        "prior":"الفترة السابقة",
        "current":"الفترة الحالية",
        "account":"الحساب",
        "debit":"مدين",
        "credit":"دائن",
        "source":"مراجع القيود",
        "trial":"ميزان المراجعة",
        "comparison":"مقارنة الأداء",
        "balance":"المركز المالي",
        "alerts":"مؤشرات تحتاج مراجعة",
        "no_alerts":"لا توجد تنبيهات وفق الحدود الحالية",
        "review":"مراجعة",
        "foot":"ONE ERP | عرض تجريبي غير مدقق",
        "no_rows":"لا توجد حركات مدرجة",
        "source_count":"عدد القيود",
        "difference":"فرق المعادلة",
        "status":"متوازن",
    },
    "en": {
        "title":"Finance Performance Dashboard",
        "company":"Company",
        "asof":"As of",
        "period":"Period",
        "assets":"Assets",
        "liabilities":"Liabilities",
        "equity":"Equity incl. earnings",
        "profit":"Period net profit",
        "revenue":"Revenue",
        "expense":"Expenses",
        "prior":"Previous period",
        "current":"Current period",
        "account":"Account",
        "debit":"Debit",
        "credit":"Credit",
        "source":"Source journal IDs",
        "trial":"Trial balance",
        "comparison":"Performance comparison",
        "balance":"Financial position",
        "alerts":"Review indicators",
        "no_alerts":"No alerts under current thresholds",
        "review":"Review",
        "foot":"ONE ERP | Experimental, unaudited view",
        "no_rows":"No movements to report",
        "source_count":"Journals",
        "difference":"Equation difference",
        "status":"Balanced",
    },
}


def _money(value: Decimal, decimals: int) -> str:
    if decimals not in (0,2,3) or not isinstance(value, Decimal) or not value.is_finite():
        raise AccountingError("Invalid reporting precision or financial value")
    return f"{value:,.{decimals}f}"


def _sources(ids, label: str) -> str:
    if not ids:
        return ""
    items = "".join("<li dir='ltr'>" + escape(str(x)) + "</li>" for x in dict.fromkeys(ids))
    return "<details><summary>" + escape(label) + " (" + str(len(tuple(dict.fromkeys(ids)))) + ")</summary><ul>" + items + "</ul></details>"


def render_finance_dashboard_html(
    data: FinanceDashboard,
    *,
    company_name: str,
    locale: str = "ar",
    minor_units: int = 2,
) -> str:
    if locale not in _TRANSLATIONS:
        raise AccountingError("Supported dashboard locale is ar or en")
    if not company_name.strip():
        raise AccountingError("Company identity required")
    if (not data.balance_sheet.balanced or not data.trial_balance.balanced or
            data.balance_sheet.tenant_id != data.tenant_id or
            data.trial_balance.tenant_id != data.tenant_id or
            data.balance_sheet.as_of != data.as_of or
            data.trial_balance.as_of != data.as_of or
            data.balance_sheet.currency != data.currency or
            data.trial_balance.currency != data.currency or
            data.comparative_income.current.tenant_id != data.tenant_id or
            data.comparative_income.current.currency != data.currency):
        raise AccountingError("Financial dashboard components disagree")
    if data.balance_sheet.accounting_equation_difference != 0:
        raise AccountingError("Out-of-balance statement cannot be shown as clean")
    c=_TRANSLATIONS[locale]
    dr="rtl" if locale=="ar" else "ltr"
    sheet=data.balance_sheet
    current=data.comparative_income.current
    old=data.comparative_income.previous

    def metric(label: str, val: Decimal, hint: str = "") -> str:
        return ("<article class='metric'><div class='metric-name'>"+escape(label)+
                "</div><div class='metric-value' dir='ltr'>"+_money(val,minor_units)+
                "</div><div class='metric-hint'>"+escape(hint)+"</div></article>")

    box="".join([
        metric(c["assets"],sheet.total_assets),
        metric(c["liabilities"],sheet.total_liabilities),
        metric(c["equity"],sheet.total_equity_including_profit),
        metric(c["profit"],current.net_profit),
    ])
    scale=max(abs(current.revenue),abs(current.expense),abs(old.revenue),
              abs(old.expense),Decimal("1"))
    def bar(label: str, name: str, value: Decimal) -> str:
        percent=int(abs(value)/scale*100)
        return ("<div class='bar-entry'><div class='bar-meta'><strong>"+escape(label)+
                "</strong><span dir='ltr'>"+_money(value,minor_units)+"</span></div>"+
                "<div class='bar-track'><div class='bar "+escape(name)+
                "' style='width:"+str(percent)+"%'></div></div></div>")
    bars=("".join([
        bar(c["current"]+" · "+c["revenue"],"revenue",current.revenue),
        bar(c["prior"]+" · "+c["revenue"],"prior",old.revenue),
        bar(c["current"]+" · "+c["expense"],"expense",current.expense),
        bar(c["prior"]+" · "+c["expense"],"prior",old.expense),
    ]))
    def cells(label: str, value: Decimal, ids=()) -> str:
        return ("<tr><td>"+escape(label)+_sources(ids,c["source"])+"</td>"+
                "<td class='num' dir='ltr'>"+_money(value,minor_units)+"</td></tr>")
    position="".join((
        cells(c["assets"],sheet.total_assets),
        cells(c["liabilities"],sheet.total_liabilities),
        cells(c["equity"],sheet.total_equity_including_profit),
        cells(c["difference"],sheet.accounting_equation_difference),
    ))
    trial=""
    for row in data.trial_balance.rows:
        trial+=("<tr><td><span class='code' dir='ltr'>"+escape(row.account_code)+
                "</span> "+escape(row.account_name)+_sources(row.source_entry_ids,c["source"])+
                "</td><td class='num' dir='ltr'>"+_money(row.debit,minor_units)+
                "</td><td class='num' dir='ltr'>"+_money(row.credit,minor_units)+
                "</td></tr>")
    if not trial:
        trial="<tr><td colspan='3'>"+escape(c["no_rows"])+"</td></tr>"
    trial+=("<tr class='total'><td>"+escape(c["trial"])+
            "</td><td class='num' dir='ltr'>"+_money(data.trial_balance.total_debit,minor_units)+
            "</td><td class='num' dir='ltr'>"+_money(data.trial_balance.total_credit,minor_units)+
            "</td></tr>")
    alerts="".join(
        "<li><strong>"+escape(insight.code)+"</strong> "+escape(insight.explanation)+
        _sources(insight.evidence,c["source"])+"</li>"
        for insight in data.comparative_income.flags
    )
    if not alerts:
        alerts="<li>"+escape(c["no_alerts"])+"</li>"
    return f'''<!doctype html>
<html lang="{locale}" dir="{dr}"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow,noarchive">
<title>ONE ERP | {escape(c["title"])}</title>
<style>
:root {{font-family:Tahoma,Arial,sans-serif;background:#eff5fb;color:#1c364e}}
* {{box-sizing:border-box}}
body {{margin:0;padding:22px}}
.dashboard {{max-width:1280px;margin:0 auto}}
header {{background:linear-gradient(115deg,#083b67,#186caa);color:white;
 border-radius:18px;padding:28px 32px;box-shadow:0 7px 22px #1c487b2b}}
.brand {{font-size:25px;font-weight:900;letter-spacing:.05em}}
h1 {{font-size:22px;margin:14px 0 8px}}
.metadata {{display:flex;gap:10px;flex-wrap:wrap;font-size:13px;opacity:.96}}
.metadata span {{background:#ffffff20;border-radius:20px;padding:6px 11px}}
.metrics {{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:13px;margin:18px 0}}
.metric,.panel {{background:white;border:1px solid #d6e5f1;border-radius:16px;
 box-shadow:0 6px 20px #1c487b0c}}
.metric {{padding:20px}}
.metric-name {{color:#51718d;font-size:13px}}
.metric-value {{font-size:clamp(20px,2.3vw,28px);font-weight:800;
 margin-top:10px;white-space:nowrap;overflow-x:auto}}
.metric-hint {{font-size:12px;color:#75899b}}
.layout {{display:grid;grid-template-columns:1fr 1fr;gap:16px}}
.panel {{padding:20px;margin-bottom:16px;min-width:0}}
.panel.wide {{grid-column:1/-1}}
h2 {{font-size:18px;margin:0 0 18px;color:#124e80}}
table {{width:100%;border-collapse:collapse;font-size:13px}}
th {{background:#eaf3fa;color:#245475;text-align:start}}
td,th {{padding:10px;border-bottom:1px solid #e4edf4;vertical-align:top}}
td.num,th.num {{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}}
tr.total {{background:#e8f2fa;font-weight:900}}
.code {{font-size:11px;color:#6c86a0;padding-inline-end:8px}}
.bar-entry {{margin:12px 0}}
.bar-meta {{display:flex;justify-content:space-between;gap:10px;font-size:13px}}
.bar-track {{height:12px;background:#e5eef7;border-radius:12px;overflow:hidden;margin-top:5px}}
.bar {{height:100%;border-radius:12px;background:#146bb1}}
.bar.prior {{background:#9ebed8}}
.bar.expense {{background:#de8d32}}
.alerts {{margin:0;padding-inline-start:22px}}
.alerts li {{margin:10px 0;line-height:1.6}}
details {{margin-top:6px;color:#426986;font-size:11px}}
summary {{cursor:pointer;max-width:max-content}}
details ul {{list-style:inside;overflow-wrap:anywhere;padding-inline-start:2px}}
footer {{text-align:center;color:#577b96;font-size:12px;padding:16px}}
@media(max-width:900px) {{.metrics {{grid-template-columns:repeat(2,minmax(0,1fr))}}}}
@media(max-width:650px) {{body {{padding:8px}}header {{padding:20px}}.metrics,.layout {{grid-template-columns:1fr}}
 .panel {{overflow-x:auto}}}}
@media print {{@page {{size:A4 landscape;margin:12mm}}
 body {{padding:0;background:white}}header {{print-color-adjust:exact;-webkit-print-color-adjust:exact}}
 .dashboard {{max-width:none}}.panel,.metric {{break-inside:avoid;box-shadow:none}}
 details:not([open]) ul {{display:none}}}}
</style></head>
<body><div class="dashboard">
<header><div class="brand">ONE ERP</div><h1>{escape(c["title"])}</h1>
<div class="metadata"><span>{escape(c["company"])}: {escape(company_name)}</span>
<span>{escape(c["period"])}: {data.from_date.isoformat()} · {data.as_of.isoformat()}</span>
<span>{escape(c["asof"])}: {data.as_of.isoformat()}</span>
<span dir="ltr">{escape(data.currency)}</span>
<span>{escape(c["source_count"])}: {data.journal_count}</span>
<span>{escape(c["status"])}</span></div></header>
<section class="metrics">{box}</section>
<div class="layout">
<section class="panel"><h2>{escape(c["comparison"])}</h2>{bars}</section>
<section class="panel"><h2>{escape(c["balance"])}</h2>
<table><tbody>{position}</tbody></table></section>
<section class="panel wide"><h2>{escape(c["alerts"])}</h2><ul class="alerts">{alerts}</ul></section>
<section class="panel wide"><h2>{escape(c["trial"])}</h2>
<table><thead><tr><th>{escape(c["account"])}</th><th class="num">{escape(c["debit"])}</th>
<th class="num">{escape(c["credit"])}</th></tr></thead><tbody>{trial}</tbody></table></section>
</div><footer>{escape(c["foot"])}</footer></div></body></html>'''
