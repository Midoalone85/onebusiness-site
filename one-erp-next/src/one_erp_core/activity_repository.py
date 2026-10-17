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
    "invoice": """
        SELECT id::text,event,actor_id,occurred_at
        FROM public.one_invoice_audit
        WHERE tenant_id=%s AND doc_id=%s
        ORDER BY occurred_at DESC,id DESC LIMIT %s OFFSET %s
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
ACTIVITY_TYPES = frozenset(_AUDIT_SQL)


def load_record_activity(connection, *, tenant_id: UUID, actor_id: UUID,
                         record_type: str, record_id: UUID,
                         limit: int = 20, offset: int = 0) -> dict:
    """Return authentic action/actor/time facts within one permitted tenant.

    In a single repeatable-read snapshot verify non-privileged SQL role,
    active company membership, and the DB-owned per-user permission decision.
    Display-time labels are never trusted as security or actor evidence.
    """
    if (not isinstance(tenant_id, UUID) or tenant_id.int == 0
        or not isinstance(actor_id, UUID) or actor_id.int == 0
        or not isinstance(record_id, UUID) or record_id.int == 0
        or record_type not in ACTIVITY_TYPES
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
            cur.execute(_AUDIT_SQL[record_type], (tenant_id,record_id,limit+1,offset))
            rows = cur.fetchall()
    more = len(rows) > limit
    items = []
    for event_id,action,event_actor,at in rows[:limit]:
        if (not isinstance(event_id,str) or not 1 <= len(event_id) <= 64
            or not isinstance(action,str) or not 1 <= len(action) <= 100
            or not isinstance(event_actor,UUID) or event_actor.int == 0
            or not isinstance(at,datetime) or at.tzinfo is None):
            raise ValueError("Invalid stored audit fact")
        items.append({
            "event_id":event_id, "action":action,
            "actor_id":str(event_actor),
            "occurred_at":at.astimezone(timezone.utc).isoformat(
                timespec="microseconds").replace("+00:00","Z"),
        })
    return {
        "module":"activity", "record_type":record_type,
        "record_id":str(record_id), "tenant_id":str(tenant_id),
        "items":items,
        "page":{"limit":limit,"offset":offset,"has_more":more,
                "next_offset":offset+limit if more else None},
        "source":"one_erp_next_postgresql", "read_only":True,
    }
