"""Bounded, read-only per-record activity timeline from persisted audit facts.

Not a global every-click tracker. The allowed sources are existing append-only
operational audit trails. Actor display names require a trusted identity directory.
Caller must provide a VERIFIED identity and a dedicated restricted tenant DB role.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

# Only fixed SQL is allowed. No user-chosen table identifiers or SQL fragments.
_AUDIT_SQL = {
    "sales_order": """
        SELECT event_id,action,actor_id,occurred_at FROM (
            SELECT 'confirmed:'||id::text AS event_id,
                   'sales_order.confirmed' AS action,
                   confirmed_by AS actor_id,confirmed_at AS occurred_at
            FROM public.one_commercial_orders
            WHERE tenant_id=%s AND id=%s AND kind='sale'
              AND status='confirmed' AND confirmed_by IS NOT NULL
              AND confirmed_at IS NOT NULL
            UNION ALL
            SELECT 'change:'||event_id::text AS event_id,
                   'sales_order.'||action AS action,
                   actor_id,occurred_at
            FROM public.one_commercial_change_audit
            WHERE tenant_id=%s AND order_id=%s AND kind='sale'
        ) AS events
        ORDER BY occurred_at DESC,event_id DESC LIMIT %s OFFSET %s
    """,
    "purchase_order": """
        SELECT event_id,action,actor_id,occurred_at FROM (
            SELECT 'confirmed:'||id::text AS event_id,
                   'purchase_order.confirmed' AS action,
                   confirmed_by AS actor_id,confirmed_at AS occurred_at
            FROM public.one_commercial_orders
            WHERE tenant_id=%s AND id=%s AND kind='purchase'
              AND status='confirmed' AND confirmed_by IS NOT NULL
              AND confirmed_at IS NOT NULL
            UNION ALL
            SELECT 'change:'||event_id::text AS event_id,
                   'purchase_order.'||action AS action,
                   actor_id,occurred_at
            FROM public.one_commercial_change_audit
            WHERE tenant_id=%s AND order_id=%s AND kind='purchase'
        ) AS events
        ORDER BY occurred_at DESC,event_id DESC LIMIT %s OFFSET %s
    """,
    "invoice": """
        SELECT event_id,action,actor_id,occurred_at FROM (
            SELECT 'issued:'||id::text AS event_id,
                   event AS action,actor_id,occurred_at
            FROM public.one_invoice_audit
            WHERE tenant_id=%s AND doc_id=%s
            UNION ALL
            SELECT 'requested:'||document_id::text,
                   'invoice.source_requested',requested_by,requested_at
            FROM public.one_invoice_source_requests
            WHERE tenant_id=%s AND document_id=%s
            UNION ALL
            SELECT 'approved:'||document_id::text,
                   'invoice.source_approved',approved_by,approved_at
            FROM public.one_invoice_source_approvals
            WHERE tenant_id=%s AND document_id=%s
        ) AS events
        ORDER BY occurred_at DESC,event_id DESC LIMIT %s OFFSET %s
    """,
    "payment": """
        SELECT id::text,event,actor_id,occurred_at
        FROM public.one_payment_audit
        WHERE tenant_id=%s AND payment_id=%s
        ORDER BY occurred_at DESC,id DESC LIMIT %s OFFSET %s
    """,
    "journal": """
        SELECT id::text,event,actor_id,occurred_at
        FROM public.one_ledger_audit
        WHERE tenant_id=%s AND entry_id=%s
        ORDER BY occurred_at DESC,id DESC LIMIT %s OFFSET %s
    """,
    "return": """
        SELECT id::text,'return.'||event,actor_id,occurred_at
        FROM public.one_return_audit
        WHERE tenant_id=%s AND job_id=%s
        ORDER BY occurred_at DESC,id DESC LIMIT %s OFFSET %s
    """,
    "stock": """
        SELECT id::text,'stock.'||movement_kind,actor_id,created_at
        FROM public.one_stock_events
        WHERE tenant_id=%s AND id=%s
        ORDER BY created_at DESC,id DESC LIMIT %s OFFSET %s
    """,
    "delivery": """
        SELECT id::text,'delivery.'||event,actor_id,happened_at
        FROM public.one_fulfillment_audit
        WHERE tenant_id=%s AND delivery_id=%s
        ORDER BY happened_at DESC,id DESC LIMIT %s OFFSET %s
    """,
    "receiving": """
        SELECT id::text,'receiving.'||event,actor_id,happened_at
        FROM public.one_receiving_audit
        WHERE tenant_id=%s AND inspection_id=%s
        ORDER BY happened_at DESC,id DESC LIMIT %s OFFSET %s
    """,
}

# Explicit SQL allowlist for confidential finance-diff reads. Never interpolate
# a browser-provided table, predicate, actor, or database reference.
_ORDER_CHANGE_DETAIL_SQL = {
    "sales_order": """
        SELECT event_id,action,actor_id,occurred_at,before_values,after_values FROM (
            SELECT 'confirmed:'||id::text AS event_id,
                   'sales_order.confirmed' AS action,
                   confirmed_by AS actor_id,confirmed_at AS occurred_at,
                   NULL::jsonb AS before_values,NULL::jsonb AS after_values
            FROM public.one_commercial_orders
            WHERE tenant_id=%s AND id=%s AND kind='sale'
              AND status='confirmed' AND confirmed_by IS NOT NULL
              AND confirmed_at IS NOT NULL
            UNION ALL
            SELECT 'change:'||event_id::text AS event_id,
                   'sales_order.'||action AS action,
                   actor_id,occurred_at,before_values,after_values
            FROM public.one_commercial_change_audit
            WHERE tenant_id=%s AND order_id=%s AND kind='sale'
        ) AS events
        ORDER BY occurred_at DESC,event_id DESC LIMIT %s OFFSET %s
    """,
    "purchase_order": """
        SELECT event_id,action,actor_id,occurred_at,before_values,after_values FROM (
            SELECT 'confirmed:'||id::text AS event_id,
                   'purchase_order.confirmed' AS action,
                   confirmed_by AS actor_id,confirmed_at AS occurred_at,
                   NULL::jsonb AS before_values,NULL::jsonb AS after_values
            FROM public.one_commercial_orders
            WHERE tenant_id=%s AND id=%s AND kind='purchase'
              AND status='confirmed' AND confirmed_by IS NOT NULL
              AND confirmed_at IS NOT NULL
            UNION ALL
            SELECT 'change:'||event_id::text AS event_id,
                   'purchase_order.'||action AS action,
                   actor_id,occurred_at,before_values,after_values
            FROM public.one_commercial_change_audit
            WHERE tenant_id=%s AND order_id=%s AND kind='purchase'
        ) AS events
        ORDER BY occurred_at DESC,event_id DESC LIMIT %s OFFSET %s
    """,
}
_CHANGE_KEYS = frozenset({'status','line_no','product_id','warehouse_id',
                          'ordered_qty','unit_price'})

ACTIVITY_TYPES = frozenset(_AUDIT_SQL)
_ORDER_VIEW_PERMISSIONS = {
    "sales_order": "sales.order.view",
    "purchase_order": "purchase.order.view",
}


def load_record_activity(connection, *, tenant_id: UUID, actor_id: UUID,
                         record_type: str, record_id: UUID,
                         limit: int = 20, offset: int = 0,
                         include_changes: bool = False) -> dict:
    """Return authentic action/actor/time facts within one permitted tenant.

    In a single repeatable-read snapshot verify non-privileged SQL role,
    active company membership, and the DB-owned per-user permission decision.
    Display-time labels are never trusted as security or actor evidence.
    """
    if (not isinstance(tenant_id, UUID) or tenant_id.int == 0
        or not isinstance(actor_id, UUID) or actor_id.int == 0
        or not isinstance(record_id, UUID) or record_id.int == 0
        or record_type not in ACTIVITY_TYPES
        or type(include_changes) is not bool
        or (include_changes and record_type not in _ORDER_CHANGE_DETAIL_SQL)
        or type(limit) is not int or not 1 <= limit <= 50
        or type(offset) is not int or not 0 <= offset <= 5000):
        raise ValueError("Bounded trusted activity query required")

    with connection.transaction():
        with connection.cursor() as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            cur.execute("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")
            role = cur.fetchone()
            if not role or bool(role[0]) or bool(role[1]):
                raise PermissionError("Privileged SQL roles cannot serve private activity")
            cur.execute("SELECT set_config('one.tenant_id',%s,true)", (str(tenant_id),))
            cur.execute("SELECT set_config('one.actor_id',%s,true)", (str(actor_id),))
            cur.execute("SELECT one_actor_has_permission(%s,%s,%s)",
                        (tenant_id,actor_id,"audit.activity.view"))
            permission = cur.fetchone()
            if permission is None or permission[0] is not True:
                raise PermissionError("Activity visibility denied")
            if record_type in _ORDER_VIEW_PERMISSIONS:
                # Audit visibility alone must not reveal restricted commercial
                # orders. Require the same module-specific source permission.
                cur.execute("SELECT one_actor_has_permission(%s,%s,%s)",
                            (tenant_id,actor_id,_ORDER_VIEW_PERMISSIONS[record_type]))
                order_allowed = cur.fetchone()
                if order_allowed is None or order_allowed[0] is not True:
                    raise PermissionError("Commercial activity visibility denied")
            if include_changes:
                # Authenticated per-user decision, independent of the right
                # to see that an event happened.
                cur.execute("SELECT one_actor_has_permission(%s,%s,%s)",
                            (tenant_id,actor_id,"audit.order_changes.view"))
                detail_permission=cur.fetchone()
                if detail_permission is None or detail_permission[0] is not True:
                    raise PermissionError("Confidential order change details denied")
            if record_type=="invoice":
                params=(tenant_id,record_id)*3+(limit+1,offset)
            elif record_type in _ORDER_VIEW_PERMISSIONS:
                params=(tenant_id,record_id)*2+(limit+1,offset)
            else:
                params=(tenant_id,record_id,limit+1,offset)
            cur.execute((_ORDER_CHANGE_DETAIL_SQL if include_changes else
                         _AUDIT_SQL)[record_type],params)
            rows = cur.fetchall()
    more = len(rows) > limit
    items = []
    for row in rows[:limit]:
        if len(row)!=(6 if include_changes else 4):
            raise ValueError("Unexpected audit result shape")
        event_id,action,event_actor,at=row[:4]
        if (not isinstance(event_id,str) or not 1 <= len(event_id) <= 64
            or not isinstance(action,str) or not 1 <= len(action) <= 100
            or not isinstance(event_actor,UUID) or event_actor.int == 0
            or not isinstance(at,datetime) or at.tzinfo is None):
            raise ValueError("Invalid stored audit fact")
        item={
            "event_id":event_id, "action":action,
            "actor_id":str(event_actor),
            "occurred_at":at.astimezone(timezone.utc).isoformat(
                timespec="microseconds").replace("+00:00","Z"),
        }
        if include_changes:
            old,new=row[4:]
            if (old is None) != (new is None):
                raise ValueError("Partial audit diff")
            if old is not None:
                for values in (old,new):
                    if (not isinstance(values,dict) or
                        not set(values).issubset(_CHANGE_KEYS) or
                        any(type(v) not in (str,int) or
                            len(str(v))>80 for v in values.values())):
                        raise ValueError("Unsafe stored audit change")
                if not event_id.startswith("change:"):
                    raise ValueError("Diff without approved audit source")
                item["changes"]={"before":old,"after":new}
        items.append(item)
    return {
        "module":"activity", "record_type":record_type,
        "record_id":str(record_id), "tenant_id":str(tenant_id),
        "items":items,
        "page":{"limit":limit,"offset":offset,"has_more":more,
                "next_offset":offset+limit if more else None},
        "source":"one_erp_next_postgresql", "read_only":True,
    }
