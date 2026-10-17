"""ONE ERP NEXT 056 | Private, source-backed operational list snapshots.

Trusted service primitive, NOT a deployed API. The frontend supplies no SQL,
connection string, actor identity, or grant. Caller must verify signed identity
and company access BEFORE opening a separately scoped tenant connection.

The DB reader refuses privileged roles, requires a live membership AND the
048/055 per-user permission decision, and reads a stable READ ONLY snapshot.
Do not copy disposable CI grants or runtime GUCs into production authorization.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from .accounting import AccountingError

_OPERATION_PERMISSIONS = {
    "sales": "sales.order.view",
    "purchases": "purchase.order.view",
    "inventory": "inventory.stock.view",
    "returns": "inventory.return.view",
    "receivables": "finance.receivables.view",
}

# Static, explicitly allowlisted statements. No source text is constructed
# from the request path, table name, sorting, filters or arbitrary JSON.
_ORDER_SQL = """
SELECT o.id,o.number,o.partner_id,o.currency,o.status,
       l.line_no,p.sku,p.name,l.ordered_qty,l.fulfilled_qty,
       l.reserved_qty,l.unit_price
FROM one_commercial_orders o
JOIN one_commercial_order_lines l
  ON l.tenant_id=o.tenant_id AND l.order_id=o.id
JOIN one_products p
  ON p.tenant_id=l.tenant_id AND p.id=l.product_id
WHERE o.tenant_id=%s AND o.kind=%s
ORDER BY o.number,o.id,l.line_no
LIMIT %s OFFSET %s
"""
_INVENTORY_SQL = """
SELECT b.warehouse_id,w.code,w.warehouse_type,b.product_id,
       p.sku,p.name,b.on_hand,b.reserved
FROM one_stock_balances b
JOIN one_warehouses w ON w.tenant_id=b.tenant_id
 AND w.id=b.warehouse_id
JOIN one_products p ON p.tenant_id=b.tenant_id
 AND p.id=b.product_id
WHERE b.tenant_id=%s
ORDER BY w.code,w.id,p.sku,p.id
LIMIT %s OFFSET %s
"""
_RETURNS_SQL = """
SELECT r.id,r.return_kind,r.status,r.quantity,r.accepted_qty,
       r.reason,r.source_event_id,r.processed_event_id,
       r.requested_at,r.reviewed_at
FROM one_return_jobs r
WHERE r.tenant_id=%s
ORDER BY r.requested_at DESC,r.id DESC
LIMIT %s OFFSET %s
"""

_AR_SQL = """
SELECT i.id,i.number,i.partner_id,i.currency,i.issued_on,i.due_on,
       i.gross_total,p.paid,c.credited
FROM public.one_invoice_docs i
LEFT JOIN LATERAL (
  SELECT coalesce(sum(a.amount),0) AS paid
  FROM public.one_payment_allocations a
  JOIN public.one_payment_docs d ON d.tenant_id=a.tenant_id
     AND d.id=a.payment_id
  WHERE a.tenant_id=i.tenant_id AND a.invoice_id=i.id
    AND d.direction='customer_receipt' AND d.status='applied'
    AND d.occurred_on<=%s AND d.applied_at::date<=%s
) p ON true
LEFT JOIN LATERAL (
  SELECT coalesce(sum(credit.gross_total),0) AS credited
  FROM public.one_invoice_docs credit
  WHERE credit.tenant_id=i.tenant_id AND credit.source_invoice_id=i.id
    AND credit.kind='customer_credit' AND credit.status='issued'
    AND credit.issued_on<=%s AND credit.approved_at::date<=%s
) c ON true
WHERE i.tenant_id=%s AND i.kind='customer_invoice'
  AND i.status='issued' AND i.issued_on<=%s
  AND i.approved_at::date<=%s
ORDER BY i.due_on,i.id
LIMIT %s OFFSET %s
"""


def _aging_row(row, as_of: date) -> dict:
    (ident,number,partner,currency,issued_on,due_on,gross,paid,credited)=row
    if (not all(isinstance(n,Decimal) and n.is_finite() and n>=0
                for n in (gross,paid,credited))
        or not isinstance(issued_on,date) or not isinstance(due_on,date)
        or not isinstance(currency,str)):
        raise AccountingError("Invalid receivables source values")
    balance=gross-paid-credited
    if balance<0:
        raise AccountingError("Overallocated invoice ledger in receivables")
    overdue=max(0,(as_of-due_on).days) if balance>0 else 0
    bucket=("internally_cleared" if balance==0 else
            "current" if overdue==0 else
            "1_30" if overdue<=30 else
            "31_60" if overdue<=60 else
            "61_90" if overdue<=90 else "over_90")
    return {
        "invoice_id":_wire(ident),"number":_wire(number),
        "partner_reference":_wire(partner),"currency":currency,
        "issued_on":_wire(issued_on),"due_on":_wire(due_on),
        "gross":_wire(gross),"applied_receipts":_wire(paid),
        "issued_credits":_wire(credited),"outstanding":_wire(balance),
        "days_overdue":overdue,"aging_bucket":bucket,
        "status":"no_open_balance_internal" if balance==0 else "open",
    }


def _wire(value):
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise AccountingError("Non-finite operational value in source")
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise AccountingError("Unsupported source field type")


def load_operations_page(
    connection, *, tenant_id: UUID, actor_id: UUID,
    module: str, limit: int = 25, offset: int = 0,
    as_of: date | None = None,
) -> dict:
    """One deterministic, bounded, permission-scoped financial operations page.

    Money and quantities are always JSON decimal STRINGS, never floats.
    Returns row-source references and has_more for cursorless pagination.
    This read does not issue orders, post journals or create stock events.
    """
    if (not isinstance(tenant_id, UUID) or not isinstance(actor_id, UUID)
        or not isinstance(module, str)
        or module not in _OPERATION_PERMISSIONS
        or type(limit) is not int or not 1 <= limit <= 50
        or type(offset) is not int or not 0 <= offset <= 5000
        or (module == "receivables" and
            (not isinstance(as_of,date) or type(as_of) is not date))
        or (module != "receivables" and as_of is not None)):
        raise ValueError("Bounded and approved operations query required")
    with connection.transaction():
        with connection.cursor() as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            cur.execute(
                "SELECT rolsuper,rolbypassrls FROM pg_roles "
                "WHERE rolname=current_user"
            )
            role = cur.fetchone()
            if not role or bool(role[0]) or bool(role[1]):
                raise PermissionError("Privileged database roles cannot read tenant data")
            cur.execute("SELECT set_config('one.tenant_id',%s,true)",(str(tenant_id),))
            cur.execute("SELECT set_config('one.actor_id',%s,true)",(str(actor_id),))
            cur.execute(
                "SELECT one_actor_has_permission(%s,%s,%s)",
                (tenant_id,actor_id,_OPERATION_PERMISSIONS[module])
            )
            allowed = cur.fetchone()
            if allowed is None or allowed[0] is not True:
                raise PermissionError("Company operation read permission denied")

            if module in ("sales","purchases"):
                cur.execute(_ORDER_SQL,(
                    tenant_id,"sale" if module=="sales" else "purchase",
                    limit+1,offset
                ))
            elif module=="inventory":
                cur.execute(_INVENTORY_SQL,(tenant_id,limit+1,offset))
            elif module=="returns":
                cur.execute(_RETURNS_SQL,(tenant_id,limit+1,offset))
            else:
                cur.execute(_AR_SQL,(
                    as_of,as_of,as_of,as_of,tenant_id,as_of,as_of,
                    limit+1,offset,
                ))
            rows = cur.fetchall()

    more = len(rows)>limit
    records = []
    for row in rows[:limit]:
        if module in ("sales","purchases"):
            keys=(
                "order_id","number","partner_reference","currency","status",
                "line_number","sku","product_name","ordered_quantity",
                "fulfilled_quantity","reserved_quantity","unit_price"
            )
            record=dict(zip(keys,(_wire(x) for x in row)))
        elif module=="inventory":
            keys=(
                "warehouse_id","warehouse_code","warehouse_type",
                "product_id","sku","product_name","on_hand","reserved"
            )
            record=dict(zip(keys,(_wire(x) for x in row)))
            on_hand,reserved=row[6],row[7]
            if (not isinstance(on_hand,Decimal)
                or not isinstance(reserved,Decimal)
                or on_hand<reserved or reserved<0):
                raise AccountingError("Unreconciled stock source")
            record["available"]=format(on_hand-reserved,"f")
        elif module=="returns":
            keys=(
                "return_id","kind","status","requested_quantity",
                "accepted_quantity","reason","source_stock_event_id",
                "processed_stock_event_id","requested_at","reviewed_at"
            )
            record=dict(zip(keys,(_wire(x) for x in row)))
        else:
            record=_aging_row(row,as_of)
        records.append(record)
    return {
        "module":module,
        "tenant_id":str(tenant_id),
        "items":records,
        "page":{"limit":limit,"offset":offset,
                "has_more":more,
                "next_offset":offset+limit if more else None},
        "read_only":True,
        "source":"one_erp_next_postgresql",
        **({"as_of":as_of.isoformat()} if module=="receivables" else {}),
    }
