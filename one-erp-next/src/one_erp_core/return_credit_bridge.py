"""Issue a draft-prototype customer credit only when backed by accepted goods.

Full outer PostgreSQL transaction rolls back the posted GL, internal credit
document and audits if source return/quantity validation fails. No government
invoice, automatic payment or merchant card refund is initiated.
"""
from __future__ import annotations

from typing import Mapping
from uuid import UUID

from .accounting import Account, AccountingError, AccountingPeriod
from .invoice_persistence import persist_invoice_with_journal
from .invoicing import (
    DocumentType, InvoiceDocument, InvoiceLedgerAccounts, InvoicePostingProposal,
)
from .tenancy import Action, Membership, can_access


def persist_return_backed_customer_credit(
    connection,
    *,
    return_job_id: UUID,
    document: InvoiceDocument,
    accounts: InvoiceLedgerAccounts,
    chart: Mapping[str, Account],
    period: AccountingPeriod,
    period_id: UUID,
    actor_id: UUID,
    memberships: tuple[Membership, ...],
    minor_units: int = 2,
) -> InvoicePostingProposal:
    """Atomic internal credit+posted GL+immutable accepted-return allocation.

    A different authenticated company administrator may inspect/accept physical
    goods; this accounting caller must independently have finance authorization.
    Never trust public request headers or SQL session GUCs as authentication.
    """
    if (not isinstance(return_job_id, UUID)
            or not isinstance(actor_id, UUID)
            or not isinstance(document, InvoiceDocument)
            or document.kind is not DocumentType.CUSTOMER_CREDIT
            or not isinstance(document.source_invoice_id, UUID)
            or len(document.lines) != 1):
        raise AccountingError("A single-line customer return credit is required")
    if not can_access(
        actor_id=actor_id, tenant_id=document.tenant_id,
        action=Action.WRITE_ACCOUNTING, memberships=memberships,
    ):
        raise AccountingError("Only authorized finance roles can issue credits")
    with connection.transaction():
        proposal = persist_invoice_with_journal(
            connection, document=document, accounts=accounts, chart=chart,
            period=period, period_id=period_id, actor_id=actor_id,
            memberships=memberships, minor_units=minor_units,
        )
        with connection.cursor() as cur:
            cur.execute("SELECT set_config('one.tenant_id',%s,true)",
                        (str(document.tenant_id),))
            cur.execute("SELECT set_config('one.actor_id',%s,true)",
                        (str(actor_id),))
            cur.execute(
                "SELECT one_link_customer_return_credit(%s,%s,%s,%s,%s)",
                (document.tenant_id,document.id,document.source_invoice_id,
                 return_job_id,actor_id),
            )
            result=cur.fetchone()
            if result is None or result[0] != document.id:
                raise AccountingError("Return-backed credit allocation refused")
    return proposal
