"""ONE ERP NEXT: real PostgreSQL commercial read models for private workspaces.

Five supported *read-only* workspaces. Caller MUST have verified the token
and tenant entitlement before opening a tenant-specific read-only DB role.
Client-provided actor UUID, database URL or SQL fragments are never accepted.
"""
from __future__ import annotations

from datetime import datetime, date
from decimal import Decimal
from typing import Any
from uuid import UUID
from .accounting import AccountingError

MODULES = ("sales", "purchases", "inventory", "returns", "refunds")
_MAX_PAGE = 100

# All SQL comes exclusively from constants. Page and company are bound params.
_QUERIES = {
    "sales": """
      SELECT o.id,o.number,o.partner_id,o.status,o.currency,
             count(l.line_no)::integer AS line_count,
             coalesce(sum(l.ordered_qty),0) AS ordered_qty,
             coalesce(sum(l.fulfilled_qty),0) AS fulfilled_qty,
             coalesce(sum(l.ordered_qty*l.unit_price),0) AS order_value
        FROM one_commercial_orders o
        LEFT JOIN one_commercial_order_lines l ON l.tenant_id=o.tenant_id
          AND l.order_id=o.id
       WHERE o.tenant_id=%s AND o.kind='sale'
       GROUP BY o.id,o.number,o.partner_id,o.status,o.currency
       ORDER BY o.number,o.id
       LIMIT 25 OFFSET %s
    """,
    "purchases": """
      SELECT o.id,o.number,o.partner_id,o.status,o.currency,
             count(l.line_no)::integer AS line_count,
             coalesce(sum(l.ordered_qty),0) AS ordered_qty,
             coalesce(sum(l.fulfilled_qty),0) AS fulfilled_qty,
             coalesce(sum(l.ordered_qty*l.unit_price),0) AS order_value
        FROM one_commercial_orders o
        LEFT JOIN one_commercial_order_lines l ON l.tenant_id=o.tenant_id
          AND l.order_id=o.id
       WHERE o.tenant_id=%s AND o.kind='purchase'
       GROUP BY o.id,o.number,o.partner_id,o.status,o.currency
       ORDER BY o.number,o.id
       LIMIT 25 OFFSET %s
    """,
    "inventory": """
      SELECT b.warehouse_id,w.name AS warehouse,
             b.product_id,p.sku,p.name AS product,
             b.on_hand,b.reserved,(b.on_hand-b.reserved) AS available,
             v.qty AS valued_qty,v.book_value,v.currency
        FROM one_stock_balances b
        JOIN one_warehouses w ON w.tenant_id=b.tenant_id
          AND w.id=b.warehouse_id
        JOIN one_products p ON p.tenant_id=b.tenant_id
          AND p.id=b.product_id
        LEFT JOIN one_stock_value_positions v
          ON v.tenant_id=b.tenant_id AND v.warehouse_id=b.warehouse_id
          AND v.product_id=b.product_id
       WHERE b.tenant_id=%s
       ORDER BY w.code,p.sku,b.product_id
       LIMIT 25 OFFSET %s
    """,
    "returns": """
      SELECT r.id,r.return_kind,r.status,r.quantity,r.accepted_qty,
             r.reason,r.source_event_id,r.processed_event_id,
             r.requested_at,r.destination_warehouse_id
        FROM one_return_jobs r
       WHERE r.tenant_id=%s
       ORDER BY r.requested_at DESC,r.id
       LIMIT 25 OFFSET %s
    """,
    "refunds": """
      SELECT c.return_job_id,c.original_invoice_id,
             c.gross_return_amount,c.ar_reduction,c.refund_liability,
             o.id AS order_id,o.status AS approval_status,
             o.approved_at,b.id AS bank_reference_id,
             b.verification_status AS bank_reference_status
        FROM one_nrv_refund_claims c
        LEFT JOIN one_customer_refund_orders o
          ON o.tenant_id=c.tenant_id AND o.return_job_id=c.return_job_id
        LEFT JOIN one_refund_bank_debit_candidates b
          ON b.tenant_id=o.tenant_id AND b.order_id=o.id
       WHERE c.tenant_id=%s
       ORDER BY c.created_at DESC,c.return_job_id
       LIMIT 25 OFFSET %s
    """,
}

_COUNTS = {
    "sales": """SELECT count(*) FROM one_commercial_orders
                 WHERE tenant_id=%s AND kind='sale'""",
    "purchases": """SELECT count(*) FROM one_commercial_orders
                     WHERE tenant_id=%s AND kind='purchase'""",
    "inventory": """SELECT count(*) FROM one_stock_balances WHERE tenant_id=%s""",
    "returns": """SELECT count(*) FROM one_return_jobs WHERE tenant_id=%s""",
    "refunds": """SELECT count(*) FROM one_nrv_refund_claims WHERE tenant_id=%s""",
}

# Fixed schema for clients, not based on arbitrary DB column exposure.
_FIELDS = {
    "sales": ("id","number","partner_id","status","currency","line_count",
              "ordered_qty","fulfilled_qty","order_value"),
    "purchases": ("id","number","partner_id","status","currency","line_count",
                  "ordered_qty","fulfilled_qty","order_value"),
    "inventory": ("warehouse_id","warehouse","product_id","sku","product",
                  "on_hand","reserved","available","valued_qty",
                  "book_value","currency"),
    "returns": ("id","return_kind","status","quantity","accepted_qty",
                "reason","source_event_id","processed_event_id",
                "requested_at","destination_warehouse_id"),
    "refunds": ("return_job_id","original_invoice_id","gross_return_amount",
                "ar_reduction","refund_liability","order_id","approval_status",
                "approved_at","bank_reference_id","bank_reference_status"),
}


def _safe_json_scalar(value: Any) -> str | int | bool | None:
    if value is None:
        return None
    if isinstance(value,(Decimal,UUID,datetime,date)):
        # Monetary/quantity decimals remain strings to prevent JS precision loss.
        return str(value) if not isinstance(value,(datetime,date)) else value.isoformat()
    if isinstance(value,(str,int,bool)):
        return value
    raise AccountingError("Unknown field returned by the database")


def read_operations_page(
    connection, *,
    tenant_id: UUID, actor_id: UUID, module: str, page: int = 0
) -> dict[str, Any]:
    """Return exactly one tenant-scoped, snapshot-consistent 25-row page.

    Fail closed for DB superusers/BYPASSRLS, absent active company members,
    missing source data, unknown modules, unknown row types, or oversized pages.
    This MUST be invoked by a backend with independently verified identity.
    """
    if (not isinstance(tenant_id, UUID) or not isinstance(actor_id,UUID)
        or not isinstance(module,str) or module not in MODULES
        or type(page) is not int or not 0 <= page <= _MAX_PAGE):
        raise AccountingError("Invalid operations workspace request")
    with connection.transaction():
        with connection.cursor() as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            cur.execute("""SELECT rolsuper,rolbypassrls FROM pg_roles
                           WHERE rolname=current_user""")
            role=cur.fetchone()
            if role is None or bool(role[0]) or bool(role[1]):
                raise AccountingError("Privileged connections cannot publish workspaces")
            cur.execute("SELECT set_config('one.tenant_id',%s,true)",(str(tenant_id),))
            cur.execute("SELECT set_config('one.actor_id',%s,true)",(str(actor_id),))
            cur.execute("""SELECT m.role,m.active,t.company_name
                             FROM one_memberships m
                             JOIN one_tenants t ON t.id=m.tenant_id
                            WHERE m.tenant_id=%s AND m.actor_id=%s""",
                        (tenant_id,actor_id))
            membership=cur.fetchone()
            if (membership is None or not membership[1] or
                membership[0] not in ("company_admin","accountant","viewer") or
                not membership[2] or not membership[2].strip()):
                raise AccountingError("Active company finance membership required")
            cur.execute(_COUNTS[module],(tenant_id,))
            total=cur.fetchone()[0]
            if not isinstance(total,int) or total<0:
                raise AccountingError("Invalid company dataset count")
            cur.execute(_QUERIES[module],(tenant_id,page*25))
            rows=cur.fetchall()
            if len(rows)>25 or any(len(row)!=len(_FIELDS[module]) for row in rows):
                raise AccountingError("Unexpected workspace result shape")
            payload=[
                {name:_safe_json_scalar(value)
                 for name,value in zip(_FIELDS[module],row)}
                for row in rows
            ]
    return {
        "tenant_id":str(tenant_id),
        "company":membership[2],
        "module":module,
        "page":page,
        "page_size":25,
        "total":total,
        "has_more":(page+1)*25<total,
        "fields":list(_FIELDS[module]),
        "items":payload,
        "mode":"read_only_source_backed",
        "settlement_notice":(
            "Bank references are unverified; refund liability is not settled"
            if module=="refunds" else None
        ),
    }
