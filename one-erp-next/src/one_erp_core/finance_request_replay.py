"""Durable same-request replay for INTERNAL invoice and payment posting.

Call only from a trusted, authenticated backend with an isolated tenant
database and an explicitly reviewed restricted SQL role. This is not a public
HTTP endpoint, ZATCA issuance, bank transfer or confirmation of bank receipt.

All reservation writes, accounting changes and result registration share
one PostgreSQL transaction. A conflicting request is never replayed.
"""
from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import json
from typing import Any, Callable
from uuid import UUID

from .accounting import AccountingError
from .tenancy import Action, Membership, can_access


@dataclass(frozen=True, slots=True)
class FinanceReplayReceipt:
    operation: str
    document_id: UUID
    replayed: bool


def _canonical(value: Any) -> Any:
    """Strict, type-preserving representation of server-validated inputs."""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return {"int": str(value)}
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise AccountingError("Non-finite financial value")
        return {"decimal": format(value, "f")}
    if isinstance(value, UUID):
        return {"uuid": str(value)}
    if isinstance(value, Enum):
        return {"enum": str(value.value), "type": type(value).__name__}
    if isinstance(value, (datetime, date)):
        return {"date": value.isoformat()}
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "dataclass": type(value).__name__,
            "fields": {f.name: _canonical(getattr(value, f.name))
                       for f in fields(value)},
        }
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    if isinstance(value, (set, frozenset)):
        members = [_canonical(v) for v in value]
        return sorted(members, key=lambda v: json.dumps(v, sort_keys=True))
    if isinstance(value, dict):
        if not all(isinstance(k, str) for k in value):
            raise AccountingError("Unsupported fingerprint map key")
        return {k: _canonical(value[k]) for k in sorted(value)}
    raise AccountingError("Unsupported financial request fingerprint value")


def _fingerprint(operation: str, payload: Any) -> str:
    encoded = json.dumps(
        {"version": 1, "operation": operation, "payload": _canonical(payload)},
        ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _run_once(
    connection, *, operation: str, document_id: UUID, tenant_id: UUID,
    actor_id: UUID, memberships: tuple[Membership, ...],
    payload: Any, write: Callable[[], UUID],
) -> FinanceReplayReceipt:
    """Atomic reservation + domain transaction + immutable result.

    The unique key (tenant_id, operation, document_id) makes same-key races
    serialize in PostgreSQL. Failed writes roll back the pending reservation.
    A changing payload or actor cannot borrow a prior successful result.
    This function does not trust browser-provided identities, and service
    authentication, per-company credentials and restricted roles are required.
    """
    if (operation not in ("internal_invoice", "internal_payment")
        or not isinstance(document_id, UUID)
        or not isinstance(tenant_id, UUID)
        or not isinstance(actor_id, UUID)
        or not can_access(
            actor_id=actor_id, tenant_id=tenant_id,
            action=Action.WRITE_ACCOUNTING, memberships=memberships,
        )):
        raise PermissionError("Verified accounting identity required")
    fingerprint = _fingerprint(operation, payload)

    with connection.transaction():
        with connection.cursor() as cur:
            cur.execute("SET LOCAL search_path = pg_catalog, public, pg_temp")
            cur.execute("SELECT set_config('one.tenant_id',%s,true)", (str(tenant_id),))
            cur.execute("SELECT set_config('one.actor_id',%s,true)", (str(actor_id),))
            # Cross-check the real database membership before reading replay
            # state, not only the caller's membership snapshot.
            cur.execute(
                "SELECT 1 FROM public.one_memberships "
                "WHERE tenant_id=%s AND actor_id=%s AND active "
                "AND role IN ('accountant','company_admin')",
                (tenant_id, actor_id),
            )
            if cur.fetchone() is None:
                raise PermissionError("Database accounting membership required")
            cur.execute(
                "INSERT INTO public.one_finance_requests "
                "(tenant_id,operation,request_id,actor_id,request_sha256) "
                "VALUES (%s,%s,%s,%s,%s) "
                "ON CONFLICT (tenant_id,operation,request_id) DO NOTHING "
                "RETURNING request_id",
                (tenant_id, operation, document_id, actor_id, fingerprint),
            )
            inserted = cur.fetchone() is not None
            if not inserted:
                cur.execute(
                    "SELECT actor_id,request_sha256,result_id "
                    "FROM public.one_finance_requests "
                    "WHERE tenant_id=%s AND operation=%s AND request_id=%s "
                    "FOR UPDATE",
                    (tenant_id, operation, document_id),
                )
                row = cur.fetchone()
                if row is None:
                    raise AccountingError("Replay reservation not visible: retry safely")
                old_actor, old_fingerprint, result_id = row
                if old_actor != actor_id or old_fingerprint != fingerprint:
                    raise AccountingError("Conflicting finance retry payload or actor")
                if result_id is None or result_id != document_id:
                    raise AccountingError("Incomplete or inconsistent finance request")
                return FinanceReplayReceipt(operation, result_id, True)

        # No external network action is permitted inside write(). It must
        # persist only internal accounting data with nested savepoints.
        result_id = write()
        if result_id != document_id:
            raise AccountingError("Finance result identity differs from request")
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE public.one_finance_requests SET result_id=%s "
                "WHERE tenant_id=%s AND operation=%s AND request_id=%s "
                "AND actor_id=%s AND request_sha256=%s AND result_id IS NULL "
                "RETURNING result_id",
                (document_id, tenant_id, operation, document_id,
                 actor_id, fingerprint),
            )
            if cur.fetchone() != (document_id,):
                raise AccountingError("Failed to seal finance request result")
    return FinanceReplayReceipt(operation, document_id, False)


def issue_invoice_once(
    connection, *, document, accounts, chart, period, period_id: UUID,
    actor_id: UUID, memberships: tuple[Membership, ...],
    minor_units: int = 2,
    goods_receipt_liability_accounts: frozenset[str] = frozenset(),
) -> FinanceReplayReceipt:
    """Issue a single *internal* invoice. Replay yields its original ID."""
    from .invoice_persistence import persist_invoice_with_journal

    def write() -> UUID:
        result = persist_invoice_with_journal(
            connection, document=document, accounts=accounts,
            chart=chart, period=period, period_id=period_id,
            actor_id=actor_id, memberships=memberships,
            minor_units=minor_units,
            goods_receipt_liability_accounts=goods_receipt_liability_accounts,
        )
        return result.document.id

    return _run_once(
        connection, operation="internal_invoice", document_id=document.id,
        tenant_id=document.tenant_id, actor_id=actor_id,
        memberships=memberships,
        payload={
            "document": document, "accounts": accounts,
            "period_id": period_id, "minor_units": minor_units,
            "goods_receipt_liability_accounts": goods_receipt_liability_accounts,
        },
        write=write,
    )


def apply_internal_payment_once(
    connection, *, payment, invoices, accounts, chart, period,
    period_id: UUID, actor_id: UUID,
    memberships: tuple[Membership, ...], minor_units: int = 2,
) -> FinanceReplayReceipt:
    """Apply one internal settlement; never asserts bank funds arrived."""
    from .payment_persistence import persist_payment_with_allocations

    def write() -> UUID:
        result = persist_payment_with_allocations(
            connection, payment=payment, invoices=invoices, accounts=accounts,
            chart=chart, period=period, period_id=period_id,
            actor_id=actor_id, memberships=memberships,
            minor_units=minor_units,
        )
        return result.request.id

    # The changing outstanding-balance snapshots are intentionally not
    # fingerprinted: they may differ after the FIRST committed settlement.
    return _run_once(
        connection, operation="internal_payment", document_id=payment.id,
        tenant_id=payment.tenant_id, actor_id=actor_id,
        memberships=memberships,
        payload={
            "payment": payment, "accounts": accounts,
            "period_id": period_id, "minor_units": minor_units,
        },
        write=write,
    )
