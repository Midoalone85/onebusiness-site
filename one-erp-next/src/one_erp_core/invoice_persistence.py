"""Atomically persist a finance document, post its journal and approve snapshot.

INTERNAL approval only, NOT ZATCA/government tax-invoice issuance.
All inputs must originate in an authenticated service and the country-specific
tax treatment must be validated by an approved country pack before calling.
"""
from __future__ import annotations

from typing import Mapping
from uuid import UUID

from .accounting import Account, AccountingError, AccountingPeriod, JournalEntry
from .invoicing import (
    DocumentType, InvoiceDocument, InvoiceLedgerAccounts, InvoicePostingProposal,
    propose_invoice_journal,
)
from .postgresql import persist_and_post
from .tenancy import Membership


def persist_invoice_with_journal(
    connection,
    *,
    document: InvoiceDocument,
    accounts: InvoiceLedgerAccounts,
    chart: Mapping[str, Account],
    period: AccountingPeriod,
    period_id: UUID,
    actor_id: UUID,
    memberships: tuple[Membership, ...],
    minor_units: int = 2,
    goods_receipt_liability_accounts: frozenset[str] = frozenset(),
) -> InvoicePostingProposal:
    """One transaction from source invoice to posted journal and sealed snapshot.

    Uses nested psycopg transaction savepoint through persist_and_post.
    A failure in any operation rolls back journal, document, lines and audit.
    Critical: caller must NOT let HTTP clients control the actor/tenant
    context or open raw database connections.
    """
    proposal = propose_invoice_journal(
        document, accounts=accounts, chart=dict(chart), period=period,
        minor_units=minor_units,
        goods_receipt_liability_accounts=goods_receipt_liability_accounts,
    )
    is_customer = document.kind in (DocumentType.CUSTOMER_INVOICE,DocumentType.CUSTOMER_CREDIT)
    control_id = accounts.receivable_id if is_customer else accounts.payable_id

    with connection.transaction():
        posted = persist_and_post(
            connection, entry=proposal.journal, period_id=period_id,
            period=period, chart=chart, actor_id=actor_id,
            memberships=memberships, minor_units=minor_units,
        )
        if posted.id != proposal.journal.id:
            raise AccountingError("Linked journal ID differs from approved document")
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('one.tenant_id',%s,true)", (str(document.tenant_id),))
            cursor.execute("SELECT set_config('one.actor_id',%s,true)", (str(actor_id),))
            cursor.execute(
                """INSERT INTO one_invoice_docs
                 (tenant_id,id,kind,number,partner_id,issued_on,due_on,
                  currency,source_invoice_id,control_account_id,journal_id,
                  net_total,tax_total,gross_total)
                 VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    document.tenant_id,document.id,document.kind.value,
                    document.number,document.partner_id,document.issued_on,
                    document.due_on,document.currency,document.source_invoice_id,
                    control_id,posted.id,proposal.totals.net,proposal.totals.tax,
                    proposal.totals.gross,
                ),
            )
            for sequence, (line, totals) in enumerate(
                zip(document.lines, proposal.totals.details), start=1
            ):
                cursor.execute(
                    """INSERT INTO one_invoice_lines
                     (tenant_id,doc_id,line_number,description,account_id,
                      quantity,unit_price,discount_amount,tax_category,tax_code,
                      tax_rate,input_tax_recoverable,base_total,tax_total)
                     VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        document.tenant_id,document.id,sequence,line.description,
                        line.account_id,line.quantity,line.unit_price,
                        line.discount_amount,line.tax_category.value,line.tax_code,
                        line.tax_rate,line.input_tax_recoverable,
                        totals.base,totals.tax,
                    ),
                )
            cursor.execute(
                "SELECT one_issue_invoice(%s,%s,%s)",
                (document.tenant_id, document.id, actor_id),
            )
            row = cursor.fetchone()
            if row is None or row[0] != document.id:
                raise AccountingError("Database did not confirm invoice snapshot")
    return proposal
