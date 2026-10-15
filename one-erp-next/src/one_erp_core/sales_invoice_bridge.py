"""Atomically issue an internal customer invoice and bind it to dispatched sale lines.

Development-only adapter. The SQL source guard authoritatively checks actual
PostgreSQL fulfillment under an order lock, preventing overbilling or stale
client-supplied quantities. This does NOT issue a government tax invoice or
make the public demo a connected application.
"""
from __future__ import annotations

from typing import Mapping
from uuid import UUID

from .accounting import Account, AccountingError, AccountingPeriod
from .invoice_persistence import persist_invoice_with_journal
from .invoicing import (
    DocumentType, InvoiceDocument, InvoiceLedgerAccounts,
    InvoicePostingProposal,
)
from .tenancy import Action, Membership, can_access


def persist_dispatch_backed_sales_invoice(
    connection,
    *,
    order_id: UUID,
    order_line_numbers: tuple[int, ...],
    document: InvoiceDocument,
    accounts: InvoiceLedgerAccounts,
    chart: Mapping[str, Account],
    period: AccountingPeriod,
    period_id: UUID,
    actor_id: UUID,
    memberships: tuple[Membership, ...],
    minor_units: int = 2,
) -> InvoicePostingProposal:
    """Post journal, issue internal invoice and link its entire line set atomically.

    Each invoice line N maps to order_line_numbers[N-1]. A later authoritative
    SQL guard compares customer/currency, original unit price, and fulfilled
    quantity less previously billed allocations, with serialized order locking.

    Caller MUST authenticate actor, tenant and company DB independently of
    browser parameters and PostgreSQL transaction-local settings.
    """
    if (not isinstance(order_id, UUID) or
            not isinstance(document, InvoiceDocument) or
            document.kind is not DocumentType.CUSTOMER_INVOICE or
            not isinstance(actor_id, UUID) or
            not isinstance(order_line_numbers, tuple) or
            not order_line_numbers or len(order_line_numbers) != len(document.lines) or
            len(order_line_numbers) > 1000 or
            any(not isinstance(n, int) or isinstance(n, bool) or n <= 0
                for n in order_line_numbers) or
            len(set(order_line_numbers)) != len(order_line_numbers)):
        raise AccountingError("Invalid invoice-to-dispatched-sale mapping")
    if not can_access(
        actor_id=actor_id, tenant_id=document.tenant_id,
        action=Action.WRITE_ACCOUNTING, memberships=memberships
    ):
        raise AccountingError("Invoice posting role is required")

    # Nested invoice writer savepoints are intentionally enclosed here:
    # a failed source allocation rolls back the invoice, GL and audit together.
    with connection.transaction():
        proposal = persist_invoice_with_journal(
            connection, document=document, accounts=accounts,
            chart=chart, period=period, period_id=period_id,
            actor_id=actor_id, memberships=memberships,
            minor_units=minor_units,
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('one.tenant_id',%s,true)",
                (str(document.tenant_id),),
            )
            cursor.execute(
                "SELECT set_config('one.actor_id',%s,true)",
                (str(actor_id),),
            )
            cursor.execute(
                "SELECT one_link_sales_invoice(%s,%s,%s,%s,%s)",
                (document.tenant_id, document.id, order_id, actor_id,
                 list(order_line_numbers)),
            )
            row = cursor.fetchone()
            if row is None or row[0] != document.id:
                raise AccountingError("Sales invoice source could not be verified")
    return proposal
