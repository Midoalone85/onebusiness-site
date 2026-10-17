"""Atomic internal payment posting and invoice allocation, not bank confirmation."""
from __future__ import annotations

from typing import Mapping
from uuid import UUID

from .accounting import Account, AccountingError, AccountingPeriod
from .payments import (
    OpenInvoice, PaymentDirection, PaymentLedgerAccounts,
    PaymentProposal, PaymentRequest, propose_payment_journal,
)
from .postgresql import persist_and_post
from .tenancy import Membership


def persist_payment_with_allocations(
    connection,
    *,
    payment: PaymentRequest,
    invoices: tuple[OpenInvoice, ...],
    accounts: PaymentLedgerAccounts,
    chart: Mapping[str, Account],
    period: AccountingPeriod,
    period_id: UUID,
    actor_id: UUID,
    memberships: tuple[Membership, ...],
    minor_units: int = 2,
) -> PaymentProposal:
    """One transaction: posted double-entry journal + allocations + audit.

    The DB checks authoritative outstanding per invoice while locking source
    invoice rows; the Python OpenInvoice snapshots alone cannot grant credit.
    The *payment* is an internally recorded settlement, not proof the bank
    transferred any funds. A separate bank reconciliation workflow is required.
    """
    proposal = propose_payment_journal(
        payment, invoices=invoices, accounts=accounts,
        chart=dict(chart), period=period, minor_units=minor_units,
    )
    control_account_id = (
        accounts.receivable_id
        if payment.direction is PaymentDirection.CUSTOMER_RECEIPT
        else accounts.payable_id
    )
    with connection.transaction():
        posted = persist_and_post(
            connection, entry=proposal.journal, period_id=period_id,
            period=period, chart=chart, actor_id=actor_id,
            memberships=memberships, minor_units=minor_units,
        )
        if posted.id != payment.id:
            raise AccountingError("Payment GL identifier mismatch")
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('one.tenant_id',%s,true)", (str(payment.tenant_id),))
            cursor.execute("SELECT set_config('one.actor_id',%s,true)", (str(actor_id),))
            cursor.execute(
                """INSERT INTO one_payment_docs
                 (tenant_id,id,partner_id,occurred_on,currency,direction,
                  amount,bank_reference,bank_account_id,control_account_id,journal_id)
                 VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (payment.tenant_id,payment.id,payment.partner_id,
                 payment.occurred_on,payment.currency,payment.direction.value,
                 payment.amount,payment.bank_reference,accounts.bank_account_id,
                 control_account_id,posted.id),
            )
            for allocation in payment.allocations:
                cursor.execute(
                    """INSERT INTO one_payment_allocations
                       (tenant_id,payment_id,invoice_id,amount)
                       VALUES (%s,%s,%s,%s)""",
                    (payment.tenant_id,payment.id,
                     allocation.invoice_id,allocation.amount),
                )
            cursor.execute(
                "SELECT one_apply_payment(%s,%s,%s)",
                (payment.tenant_id,payment.id,actor_id),
            )
            row = cursor.fetchone()
            if row is None or row[0] != payment.id:
                raise AccountingError("Database failed to apply payment")
    return proposal
