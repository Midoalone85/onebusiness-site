"""PostgreSQL source loader for independently approved INTERNAL invoice posting.

A source request is an immutable, reviewer-approved business snapshot in
migration 059; no browser-provided amount, account, period or actor is used.
Loader intentionally rejects payment until its independently controlled
source/approval workflow and bank semantics are implemented.

Not an official ZATCA invoice. Production needs isolated DB routing, verified
identity, least-privilege grants, validated Saudi/local tax policy, audit and
operator acceptance before serving this route.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
import re
from uuid import UUID

from .accounting import Account, AccountKind, AccountingPeriod
from .finance_replay_commands_wsgi import TrustedInvoiceSource
from .invoicing import (
    InvoiceDocument, InvoiceLedgerAccounts, InvoiceLine, DocumentType, TaxCategory,
)
from .tenancy import Membership, Role

_DECIMAL = re.compile(r"-?(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{1,6})?\Z")
_ISO_CURRENCY = re.compile(r"[A-Z]{3}\Z")
_MAX_LINES = 100


def _fields(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise PermissionError("Approved finance source contract invalid")
    return value


def _text(value, *, limit=200):
    if (not isinstance(value, str) or not 0 < len(value) <= limit
        or not value.strip() or any(ord(c) < 32 for c in value)):
        raise PermissionError("Invalid approved source string")
    return value


def _decimal(value):
    if not isinstance(value, str) or not _DECIMAL.fullmatch(value):
        raise PermissionError("Approved finance amounts must be exact strings")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise PermissionError("Invalid approved source decimal") from exc
    if not result.is_finite():
        raise PermissionError("Invalid approved source numeric value")
    return result


def _date(value):
    if not isinstance(value, str) or len(value) != 10:
        raise PermissionError("Invalid approved source date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise PermissionError("Invalid approved source date") from exc
    if parsed.isoformat() != value:
        raise PermissionError("Invalid approved source date")
    return parsed


def _uuid(value):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) != 36:
        raise PermissionError("Invalid approved source UUID")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise PermissionError("Invalid approved source UUID") from exc
    if str(parsed) != value:
        raise PermissionError("Noncanonical approved source UUID")
    return parsed


def _invoice_from_snapshot(payload, *, tenant, document):
    _fields(payload, (
        "kind", "number", "partner_id", "issued_on", "due_on", "currency",
        "source_invoice_id", "accounts", "lines",
    ))
    currency = payload["currency"]
    if not isinstance(currency, str) or not _ISO_CURRENCY.fullmatch(currency):
        raise PermissionError("Invalid approved currency")
    try:
        kind = DocumentType(payload["kind"])
    except (ValueError, TypeError) as exc:
        raise PermissionError("Invalid invoice document type") from exc
    acct = _fields(payload["accounts"], (
        "receivable_id", "payable_id", "output_vat_id", "input_vat_id",
    ))
    accounts = InvoiceLedgerAccounts(**{
        key: _text(value,limit=120) for key,value in acct.items()
    })
    entries = payload["lines"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= _MAX_LINES:
        raise PermissionError("Invalid approved invoice line count")
    lines = []
    for obj in entries:
        _fields(obj, (
            "description", "account_id", "quantity", "unit_price",
            "discount_amount", "tax_category", "tax_rate", "tax_code",
            "input_tax_recoverable",
        ))
        if not isinstance(obj["input_tax_recoverable"], bool):
            raise PermissionError("Invalid approved tax recovery state")
        try:
            tax_category = TaxCategory(obj["tax_category"])
        except (TypeError, ValueError) as exc:
            raise PermissionError("Invalid approved tax category") from exc
        lines.append(InvoiceLine(
            description=_text(obj["description"],limit=500),
            account_id=_text(obj["account_id"],limit=120),
            quantity=_decimal(obj["quantity"]),
            unit_price=_decimal(obj["unit_price"]),
            discount_amount=_decimal(obj["discount_amount"]),
            tax_category=tax_category,
            tax_rate=_decimal(obj["tax_rate"]),
            tax_code=_text(obj["tax_code"],limit=64),
            input_tax_recoverable=obj["input_tax_recoverable"],
        ))
    source_id = _uuid(payload["source_invoice_id"])
    return (
        InvoiceDocument(
            document, tenant, _text(payload["number"],limit=120),
            _text(payload["partner_id"],limit=120),
            kind, _date(payload["issued_on"]), _date(payload["due_on"]),
            currency, tuple(lines), source_id,
        ),
        accounts,
    )


class ApprovedPostgresFinanceSourceLoader:
    """Read-only loader of approved snapshots; requires outer transaction.

    The authenticated server passed tenant/actor are bound as transaction-local
    context. All SQL is fixed and parameters are bound. The loader never
    approves drafts, changes accounts or creates payment instructions.
    """

    def load_invoice(
        self, connection, *, tenant_id: UUID, document_id: UUID, actor_id: UUID,
    ) -> TrustedInvoiceSource:
        if not all(isinstance(v, UUID) for v in (tenant_id,document_id,actor_id)):
            raise PermissionError("Authenticated finance source required")
        with connection.cursor() as cur:
            cur.execute("SET LOCAL search_path = pg_catalog, public, pg_temp")
            cur.execute("SELECT set_config('one.tenant_id',%s,true)", (str(tenant_id),))
            cur.execute("SELECT set_config('one.actor_id',%s,true)", (str(actor_id),))
            cur.execute(
                """SELECT role FROM public.one_memberships
                   WHERE tenant_id=%s AND actor_id=%s AND active
                     AND role IN ('company_admin','accountant')""",
                (tenant_id,actor_id),
            )
            row = cur.fetchone()
            if row is None:
                raise PermissionError("Current finance membership required")
            membership = Membership(actor_id, tenant_id, Role(row[0]))
            cur.execute(
                """SELECT r.period_id,r.payload
                   FROM public.one_invoice_source_requests r
                   JOIN public.one_invoice_source_approvals a
                     ON a.tenant_id=r.tenant_id
                    AND a.document_id=r.document_id
                   WHERE r.tenant_id=%s AND r.document_id=%s
                     AND r.requested_by<>a.approved_by
                   FOR SHARE OF r,a""",
                (tenant_id,document_id),
            )
            row=cur.fetchone()
            if row is None:
                raise PermissionError("Independent approved source required")
            period_id,payload=row
            doc,accounts=_invoice_from_snapshot(
                payload, tenant=tenant_id, document=document_id,
            )
            cur.execute(
                """SELECT begins,ends_exclusive,closed
                   FROM public.one_periods
                   WHERE tenant_id=%s AND id=%s""",
                (tenant_id,period_id),
            )
            record=cur.fetchone()
            if record is None:
                raise PermissionError("Approved period not provisioned")
            period=AccountingPeriod(tenant_id,*record)
            cur.execute(
                """SELECT minor_units FROM public.one_currency_precision
                   WHERE code=%s""", (doc.currency,),
            )
            row=cur.fetchone()
            if row is None or row[0] not in (0,2,3):
                raise PermissionError("Unsupported currency precision")
            minor_units=int(row[0])
            cur.execute(
                """SELECT id,code,name,kind,active,posting_allowed
                   FROM public.one_accounts
                   WHERE tenant_id=%s ORDER BY id""", (tenant_id,),
            )
            chart={
                key: Account(key,tenant_id,code,name,AccountKind(kind),active,allowed)
                for key,code,name,kind,active,allowed in cur.fetchall()
            }
            cur.execute(
                """SELECT order_id,order_line_nos
                     FROM public.one_sales_invoice_source_links
                    WHERE tenant_id=%s AND invoice_id=%s""",
                (tenant_id,document_id),
            )
            source_link=cur.fetchone()
            sales_order_id=source_link[0] if source_link is not None else None
            sales_line_nos=tuple(source_link[1]) if source_link is not None else ()
        return TrustedInvoiceSource(
            document=doc, accounts=accounts, chart=chart,
            period=period, period_id=period_id,
            memberships=(membership,), minor_units=minor_units,
            sales_order_id=sales_order_id,
            sales_order_line_nos=sales_line_nos,
        )

    def load_payment(
        self, connection, *, tenant_id: UUID, document_id: UUID, actor_id: UUID,
    ):
        # Fail closed rather than invent a bank source or simulate balances.
        raise PermissionError("Payment approved-source loader is not enabled")
