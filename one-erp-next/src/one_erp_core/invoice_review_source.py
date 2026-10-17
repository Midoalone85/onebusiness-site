"""Read-only source-backed independent invoice reviewer evidence (062).

INTERNAL accounting only. All finance identity, tenant routing and SQL role
selection must originate from the trusted server; never a browser parameter.
A dedicated non-superuser, non-BYPASSRLS SQL login with explicit SELECT and
permission-function EXECUTE grants is required at deployment. Not deployed.
"""
from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from .approved_finance_sources import _invoice_from_snapshot
from .invoicing import DocumentType, calculate_invoice


def load_invoice_review_summary(connection, *, tenant_id: UUID,
                                actor_id: UUID, document_id: UUID) -> dict:
    """Get verified, immutable unapproved commercial invoice evidence.

    This is a review snapshot, not an approval or a legal invoice. The POST
    independently rechecks current membership, permission and maker/checker
    under the transaction, so a stale browser screen cannot confer authority.
    """
    if not all(isinstance(x,UUID) and x.int > 0
               for x in (tenant_id,actor_id,document_id)):
        raise PermissionError("Authenticated reviewer and source required")
    with connection.transaction():
        with connection.cursor() as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            cur.execute("SET LOCAL search_path = pg_catalog, public, pg_temp")
            cur.execute("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")
            db_role=cur.fetchone()
            if not db_role or db_role[0] or db_role[1]:
                raise PermissionError("Restricted finance reader SQL role required")
            cur.execute("SELECT set_config('one.tenant_id',%s,true)",(str(tenant_id),))
            cur.execute("SELECT set_config('one.actor_id',%s,true)",(str(actor_id),))
            cur.execute(
                "SELECT public.one_actor_has_permission(%s,%s,%s)",
                (tenant_id,actor_id,"finance.invoice_source.review"),
            )
            permission=cur.fetchone()
            if permission is None or permission[0] is not True:
                raise PermissionError("Invoice reviewer grant missing")
            cur.execute(
                """SELECT r.requested_by,r.payload,r.period_id,
                          l.order_id,l.order_line_nos,
                          i.number,i.issued_on,i.due_on,i.tax_category,i.tax_rate,
                          i.tax_code,i.tax_policy_reference,i.tax_policy_reviewed_by,
                          i.receivable_account_id,i.payable_account_id,
                          i.output_vat_account_id,i.input_vat_account_id,
                          o.currency,o.partner_id
                   FROM public.one_invoice_source_requests r
                   JOIN public.one_sales_invoice_source_links l
                     ON l.tenant_id=r.tenant_id AND l.invoice_id=r.document_id
                   JOIN public.one_sales_invoice_intents i
                     ON i.tenant_id=r.tenant_id AND i.invoice_id=r.document_id
                    AND i.order_id=l.order_id AND i.period_id=r.period_id
                   JOIN public.one_commercial_orders o
                     ON o.tenant_id=i.tenant_id AND o.id=i.order_id
                    AND o.kind='sale' AND o.status='confirmed'
                   JOIN public.one_periods p
                     ON p.tenant_id=r.tenant_id AND p.id=r.period_id
                    AND NOT p.closed
                   LEFT JOIN public.one_invoice_source_approvals a
                     ON a.tenant_id=r.tenant_id AND a.document_id=r.document_id
                   WHERE r.tenant_id=%s AND r.document_id=%s
                     AND a.document_id IS NULL""",
                (tenant_id,document_id),
            )
            source=cur.fetchone()
            if source is None:
                raise PermissionError("Unapproved commercial source unavailable")
            (requested,payload,period,order,line_nos,number,issued,due,
             category,rate,tax_code,tax_policy,policy_reviewer,
             receivable,payable,output_vat,input_vat,currency,partner)=source
            if (requested==actor_id or requested==policy_reviewer or
                not isinstance(tax_policy,str) or not tax_policy.strip()):
                raise PermissionError("Independent invoice reviewer required")
            cur.execute(
                """SELECT actor_id FROM public.one_memberships
                   WHERE tenant_id=%s AND active AND actor_id=ANY(%s)
                     AND role IN ('accountant','company_admin')""",
                (tenant_id,[requested,policy_reviewer]),
            )
            active={row[0] for row in cur.fetchall()}
            if requested not in active or policy_reviewer not in active:
                raise PermissionError("Prepared-source review became inactive")
            cur.execute(
                "SELECT minor_units FROM public.one_currency_precision WHERE code=%s",
                (currency,),
            )
            units=cur.fetchone()
            if not units or units[0] not in (0,2,3):
                raise PermissionError("Unsupported internal currency precision")
            minor_units=units[0]

    document,accounts=_invoice_from_snapshot(
        payload,tenant=tenant_id,document=document_id,
    )
    if (document.kind is not DocumentType.CUSTOMER_INVOICE or
        document.number!=number or document.issued_on!=issued or
        document.due_on!=due or document.currency!=currency or
        document.partner_id!=partner or
        document.source_invoice_id is not None or
        accounts.receivable_id!=receivable or accounts.payable_id!=payable or
        accounts.output_vat_id!=output_vat or
        accounts.input_vat_id!=input_vat or
        not isinstance(line_nos,(tuple,list)) or
        len(line_nos)!=len(document.lines) or not 1<=len(line_nos)<=100 or
        any(type(n) is not int or n<=0 for n in line_nos) or
        len(set(line_nos))!=len(line_nos) or
        any(line.tax_category.value!=category or
            line.tax_rate!=rate or line.tax_code!=tax_code
            for line in document.lines)):
        raise PermissionError("Invoice source and sale intent do not reconcile")
    totals=calculate_invoice(document,minor_units=minor_units)
    if not isinstance(totals.gross,Decimal) or totals.gross<=0:
        raise PermissionError("Unreconciled invoice totals")

    lines=[]
    for number,line,calculated in zip(line_nos,document.lines,totals.details):
        lines.append({
            "line_number":number,
            "description":line.description,
            "quantity":format(line.quantity,"f"),
            "unit_price":format(line.unit_price,"f"),
            "discount_amount":format(line.discount_amount,"f"),
            "tax_category":line.tax_category.value,
            "tax_rate":format(line.tax_rate,"f"),
            "tax_code":line.tax_code,
            "net":format(calculated.base,"f"),
            "tax":format(calculated.tax,"f"),
            "gross":format(calculated.total,"f"),
        })
    return {
        "tenant":str(tenant_id),"document_id":str(document_id),
        "reviewable":True,"source":"verified_internal_postgresql",
        "fields":[
            {"label":"Invoice number","value":document.number},
            {"label":"Customer reference","value":document.partner_id},
            {"label":"Order reference","value":str(order)},
            {"label":"Period reference","value":str(period)},
            {"label":"Issue date","value":document.issued_on.isoformat()},
            {"label":"Due date","value":document.due_on.isoformat()},
            {"label":"Currency","value":document.currency},
            {"label":"Internal tax policy","value":tax_policy},
            {"label":"Net","value":format(totals.net,"f")},
            {"label":"Tax (internal)","value":format(totals.tax,"f")},
            {"label":"Gross","value":format(totals.gross,"f")},
        ],
        "lines":lines,
    }
