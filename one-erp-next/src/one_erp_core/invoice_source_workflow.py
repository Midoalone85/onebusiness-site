"""Internal maker/checker service for immutable invoice source lifecycle (059).

No public drafting route, Odoo link, bank payment or official ZATCA invoice.
Caller supplies only a *trusted server builder*, verified OIDC identity and a
pinned tenant connection. Never pass browser-supplied invoice snapshot/SQL,
actor ID, account mapping or amounts to the builder.

Database triggers enforce append-only, independent approval, current active
finance membership and tenant RLS. This layer additionally enforces all
privileges before accepting a request, and returns only a reference UUID.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Mapping, Protocol
from uuid import UUID

from .approved_finance_sources import _invoice_from_snapshot
from .finance_http_boundary import AuthenticatedFinanceIdentity


class TrustedInvoiceSourceBuilder(Protocol):
    """Only service-side, reviewed source-of-truth adapter may implement this.

    Must freeze an immutable business request using persisted source records,
    authorized period and approved chart, never client POST attributes.
    """
    def build(self, connection, *, tenant_id: UUID, document_id: UUID,
              actor_id: UUID) -> "BuiltInvoiceSource": ...


@dataclass(frozen=True, slots=True)
class BuiltInvoiceSource:
    period_id: UUID
    snapshot: Mapping[str, object]
    sales_order_id: UUID | None = None
    sales_order_line_nos: tuple[int, ...] = ()


def _authorized(identity, tenant_id, document_id):
    if (not isinstance(identity, AuthenticatedFinanceIdentity) or
        not isinstance(identity.actor_id, UUID) or
        not isinstance(identity.allowed_tenants, frozenset) or
        not all(isinstance(t, UUID) for t in identity.allowed_tenants) or
        not isinstance(tenant_id, UUID) or not isinstance(document_id, UUID) or
        tenant_id not in identity.allowed_tenants or
        tenant_id.int == 0 or document_id.int == 0):
        raise PermissionError("Verified invoice source identity required")


def _assert_accountant(cur, *, tenant_id, actor_id):
    cur.execute(
        """SELECT 1 FROM public.one_memberships
           WHERE tenant_id=%s AND actor_id=%s AND active
             AND role IN ('company_admin','accountant')""",
        (tenant_id,actor_id),
    )
    if cur.fetchone() is None:
        raise PermissionError("Current finance maker/checker membership required")


def _bind_actor(cur, *, tenant_id, actor_id):
    # Session settings only inside the started, bounded SQL transaction.
    # Only the server may choose actor and tenant from independently verified
    # credentials; PostgreSQL GUC variables alone are NOT authentication.
    cur.execute("SET LOCAL search_path = pg_catalog, public, pg_temp")
    cur.execute("SELECT set_config('one.tenant_id',%s,true)",(str(tenant_id),))
    cur.execute("SELECT set_config('one.actor_id',%s,true)",(str(actor_id),))
    _assert_accountant(cur,tenant_id=tenant_id,actor_id=actor_id)


def create_trusted_invoice_source(
    connection, *, identity: AuthenticatedFinanceIdentity,
    tenant_id: UUID, document_id: UUID,
    builder: TrustedInvoiceSourceBuilder,
) -> UUID:
    """Create one immutable source from a trusted persisted-source adapter.

    Pure client operations never expose this method. The builder MUST use
    database records to determine amounts and mappings. No self approval.
    """
    _authorized(identity,tenant_id,document_id)
    if builder is None or not callable(getattr(builder, "build", None)):
        raise PermissionError("Approved immutable source builder required")
    with connection.transaction():
        with connection.cursor() as cur:
            _bind_actor(cur,tenant_id=tenant_id,actor_id=identity.actor_id)
            built=builder.build(
                connection,tenant_id=tenant_id,document_id=document_id,
                actor_id=identity.actor_id,
            )
            if (not isinstance(built,BuiltInvoiceSource) or
                not isinstance(built.period_id,UUID) or
                built.period_id.int==0 or
                not isinstance(built.snapshot,Mapping)):
                raise PermissionError("Trusted finance source snapshot required")
            # Validate exact finance source contract *before* making any write.
            snapshot=dict(built.snapshot)
            _invoice_from_snapshot(snapshot,tenant=tenant_id,document=document_id)
            try:
                frozen=json.dumps(
                    snapshot,ensure_ascii=False,allow_nan=False,
                    sort_keys=True,separators=(",",":"),
                )
            except (TypeError, ValueError, OverflowError) as error:
                raise PermissionError("Approved source must be exact JSON") from error
            if not 2 <= len(frozen.encode("utf-8")) <= 32768:
                raise PermissionError("Approved source payload outside limits")
            cur.execute(
                """SELECT 1 FROM public.one_periods
                   WHERE tenant_id=%s AND id=%s AND NOT closed""",
                (tenant_id,built.period_id),
            )
            if cur.fetchone() is None:
                raise PermissionError("Source period unavailable or closed")
            if built.sales_order_id is not None:
                line_nos=built.sales_order_line_nos
                if (not isinstance(built.sales_order_id,UUID) or
                    built.sales_order_id.int==0 or
                    not isinstance(line_nos,tuple) or
                    not 1<=len(line_nos)<=100 or
                    any(type(n) is not int or n<=0 for n in line_nos) or
                    len(set(line_nos))!=len(line_nos) or
                    len(snapshot["lines"])!=len(line_nos)):
                    raise PermissionError("Invalid immutable source lineage")
            elif built.sales_order_line_nos:
                raise PermissionError("Source lineage must specify order")
            cur.execute(
                """INSERT INTO public.one_invoice_source_requests
                   (tenant_id,document_id,period_id,requested_by,payload)
                   VALUES(%s,%s,%s,%s,%s::jsonb)""",
                (tenant_id,document_id,built.period_id,
                 identity.actor_id,frozen),
            )
            if built.sales_order_id is not None:
                cur.execute(
                    """INSERT INTO public.one_sales_invoice_source_links
                       (tenant_id,invoice_id,order_id,order_line_nos,actor_id)
                       VALUES(%s,%s,%s,%s,%s)""",
                    (tenant_id,document_id,built.sales_order_id,
                     list(built.sales_order_line_nos),identity.actor_id),
                )
    return document_id


def independently_approve_invoice_source(
    connection, *, identity: AuthenticatedFinanceIdentity,
    tenant_id: UUID, document_id: UUID,
) -> UUID:
    """Independent accounting reviewer can approve by reference only.

    There is no user-supplied source content or period/account override.
    Unique DB key refuses duplicate approvals; mutation forbidden by trigger.
    """
    _authorized(identity,tenant_id,document_id)
    with connection.transaction():
        with connection.cursor() as cur:
            _bind_actor(cur,tenant_id=tenant_id,actor_id=identity.actor_id)
            cur.execute(
                """SELECT requested_by FROM public.one_invoice_source_requests
                   WHERE tenant_id=%s AND document_id=%s FOR SHARE""",
                (tenant_id,document_id),
            )
            requested=cur.fetchone()
            if requested is None or requested[0]==identity.actor_id:
                raise PermissionError("Independent prepared source required")
            cur.execute(
                """INSERT INTO public.one_invoice_source_approvals
                   (tenant_id,document_id,approved_by)
                   VALUES(%s,%s,%s)""",
                (tenant_id,document_id,identity.actor_id),
            )
    return document_id
