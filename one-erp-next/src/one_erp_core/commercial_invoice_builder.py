"""Build a frozen internal customer invoice from *actual* dispatched sale rows.

Never trust a browser invoice amount, quantity, order ID or tax rate. The
persisted, maker-created one_sales_invoice_intents record supplies the exact
sale reference and separately reviewed *internal* accounting tax profile.
The builder looks up issued allocations and freezes ONLY unbilled deliveries.

This is NOT a Saudi legal/ZATCA fiscal-document issuer. Tax decisions,
customer tax identity and compliance remain separate country-pack gates.
"""
from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from .invoice_source_workflow import BuiltInvoiceSource


class PostgresDispatchedSalesInvoiceBuilder:
    """Server-owned, fail-closed source adapter for one recorded sale intent.

    Caller MUST enter an authenticated transaction and set tenant/actor
    context before invoking. Database roles still need granular production
    GRANTs. The order is locked before line snapshots, following the existing
    order -> line -> allocations lock hierarchy.
    """

    def build(self, connection, *, tenant_id: UUID,
              document_id: UUID, actor_id: UUID) -> BuiltInvoiceSource:
        if not all(isinstance(v, UUID) and v.int > 0
                   for v in (tenant_id,document_id,actor_id)):
            raise PermissionError("Verified invoice identity required")
        with connection.cursor() as cur:
            cur.execute(
                """SELECT order_id,period_id,number,issued_on,due_on,
                          receivable_account_id,payable_account_id,
                          revenue_account_id,output_vat_account_id,
                          input_vat_account_id,tax_category,tax_rate,tax_code,
                          tax_policy_reference,tax_policy_reviewed_by,requested_by
                     FROM public.one_sales_invoice_intents
                    WHERE tenant_id=%s AND invoice_id=%s
                    FOR SHARE""",
                (tenant_id,document_id),
            )
            intent=cur.fetchone()
            if intent is None:
                raise PermissionError("Immutable sales invoice intent required")
            (order_id,period_id,number,issued_on,due_on,
             receivable,payable,revenue,output_vat,input_vat,
             category,rate,tax_code,review_ref,reviewer,maker)=intent
            if maker != actor_id or reviewer == actor_id:
                raise PermissionError("Authenticated maker and independent tax reviewer required")
            cur.execute(
                """SELECT 1 FROM public.one_memberships
                   WHERE tenant_id=%s AND actor_id=%s AND active
                     AND role IN ('accountant','company_admin')""",
                (tenant_id,reviewer),
            )
            if cur.fetchone() is None:
                raise PermissionError("Accounting tax review was revoked")
            if not isinstance(review_ref,str) or len(review_ref.strip())<6:
                raise PermissionError("Reviewed internal tax policy reference missing")
            cur.execute(
                """SELECT partner_id,currency,status,confirmed_at
                     FROM public.one_commercial_orders
                    WHERE tenant_id=%s AND id=%s AND kind='sale'
                    FOR UPDATE""",
                (tenant_id,order_id),
            )
            row=cur.fetchone()
            if row is None or row[2] != "confirmed":
                raise PermissionError("Confirmed sales order required")
            partner,currency,_,confirmed_at=row
            if confirmed_at is None or issued_on < confirmed_at.date():
                raise PermissionError("Invoice cannot predate confirmed sale")
            cur.execute(
                """SELECT minor_units FROM public.one_currency_precision
                    WHERE code=%s""",
                (currency,),
            )
            units=cur.fetchone()
            if units is None or units[0] not in (0,2,3):
                raise PermissionError("Currency precision not reviewed")
            scale=Decimal(1).scaleb(-units[0])
            if not isinstance(rate,Decimal) or not rate.is_finite():
                raise PermissionError("Invalid reviewed tax rate")
            cur.execute(
                """SELECT l.line_no,p.name,l.fulfilled_qty,l.unit_price,
                          COALESCE((
                            SELECT sum(a.billed_qty)
                              FROM public.one_sales_invoice_allocations a
                             WHERE a.tenant_id=l.tenant_id
                               AND a.order_id=l.order_id
                               AND a.order_line_no=l.line_no
                          ),0)
                     FROM public.one_commercial_order_lines l
                     JOIN public.one_products p
                       ON p.tenant_id=l.tenant_id AND p.id=l.product_id
                    WHERE l.tenant_id=%s AND l.order_id=%s
                    ORDER BY l.line_no
                    FOR UPDATE OF l""",
                (tenant_id,order_id),
            )
            rows=cur.fetchall()
            if not rows or len(rows)>100:
                raise PermissionError("Invalid sales invoice line count")
            unbilled=[]
            mapping=[]
            for line_no,product_name,fulfilled,price,billed in rows:
                remaining=fulfilled-billed
                if remaining < 0 or remaining>fulfilled:
                    raise PermissionError("Order invoice quantity reconciliation failed")
                if remaining==0:
                    continue
                if (not isinstance(remaining,Decimal) or
                    not isinstance(price,Decimal) or
                    not remaining.is_finite() or not price.is_finite() or
                    remaining<=0 or remaining != remaining.quantize(Decimal("0.001")) or
                    price<=0 or price != price.quantize(scale)):
                    raise PermissionError("Invalid unbilled dispatched source")
                unbilled.append({
                    "description":product_name,
                    "account_id":revenue,
                    "quantity":format(remaining,"f"),
                    "unit_price":format(price,"f"),
                    "discount_amount":"0",
                    "tax_category":category,
                    "tax_rate":format(rate,"f"),
                    "tax_code":tax_code,
                    "input_tax_recoverable":False,
                })
                mapping.append(line_no)
            if not unbilled:
                raise PermissionError("No unbilled dispatched stock")
            # A financial snapshot has no authority to invent tax treatment.
            # Fields above came from an immutable separately-reviewed intent.
            snapshot={
                "kind":"customer_invoice","number":number,
                "partner_id":partner,"issued_on":issued_on.isoformat(),
                "due_on":due_on.isoformat(),"currency":currency,
                "source_invoice_id":None,
                "accounts":{
                    "receivable_id":receivable,"payable_id":payable,
                    "output_vat_id":output_vat,"input_vat_id":input_vat,
                },
                "lines":unbilled,
            }
            return BuiltInvoiceSource(
                period_id=period_id,snapshot=snapshot,
                sales_order_id=order_id,sales_order_line_nos=tuple(mapping),
            )
