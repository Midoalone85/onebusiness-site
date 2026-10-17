"""Payment allocation policy and draft GL entries for invoice settlements.

This layer NEVER confirms bank receipt, changes invoice balances, marks invoices
paid, or posts ledger entries. Those operations require a DB transaction,
approved identities and reconciled payment/receivable subledger records.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from uuid import UUID

from .accounting import (
    Account, AccountKind, AccountingError, AccountingPeriod,
    JournalEntry, JournalLine, validate_entry,
)


class PaymentDirection(str, Enum):
    CUSTOMER_RECEIPT = "customer_receipt"
    VENDOR_DISBURSEMENT = "vendor_disbursement"


@dataclass(frozen=True, slots=True)
class OpenInvoice:
    id: UUID
    tenant_id: UUID
    partner_id: str
    currency: str
    issued_on: date
    outstanding: Decimal
    is_posted: bool
    is_cancelled: bool = False


@dataclass(frozen=True, slots=True)
class PaymentAllocation:
    invoice_id: UUID
    amount: Decimal


@dataclass(frozen=True, slots=True)
class PaymentRequest:
    id: UUID
    tenant_id: UUID
    partner_id: str
    occurred_on: date
    currency: str
    direction: PaymentDirection
    amount: Decimal
    allocations: tuple[PaymentAllocation, ...]
    bank_reference: str


@dataclass(frozen=True, slots=True)
class PaymentLedgerAccounts:
    bank_account_id: str
    receivable_id: str
    payable_id: str


@dataclass(frozen=True, slots=True)
class PaymentProposal:
    request: PaymentRequest
    journal: JournalEntry
    allocated_total: Decimal


def _validated_decimal(value: Decimal, *, precision: Decimal) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise AccountingError("Payment values must be positive finite Decimal")
    if value != value.quantize(precision):
        raise AccountingError("Payment precision incompatible with currency")
    return value


def propose_payment_journal(
    payment: PaymentRequest,
    *,
    invoices: tuple[OpenInvoice, ...],
    accounts: PaymentLedgerAccounts,
    chart: dict[str, Account],
    period: AccountingPeriod,
    minor_units: int = 2,
) -> PaymentProposal:
    if minor_units not in (0, 2, 3):
        raise AccountingError("Unsupported currency minor units")
    if len(payment.currency) != 3 or payment.currency != payment.currency.upper() or not payment.currency.isascii() or not payment.currency.isalpha():
        raise AccountingError("Invalid currency")
    if not payment.partner_id.strip() or not payment.bank_reference.strip():
        raise AccountingError("Party and bank reference required")
    if not payment.allocations:
        raise AccountingError("Payment must have at least one allocated invoice")
    unit = Decimal(1).scaleb(-minor_units)
    total = _validated_decimal(payment.amount, precision=unit)
    invoices_by_id = {inv.id: inv for inv in invoices}
    if len(invoices_by_id) != len(invoices):
        raise AccountingError("Duplicate source invoices supplied")
    if any(inv.tenant_id != payment.tenant_id for inv in invoices):
        raise AccountingError("Cross-company invoice data supplied")
    if len({a.invoice_id for a in payment.allocations}) != len(payment.allocations):
        raise AccountingError("One invoice may be allocated only once per payment")
    allocated = Decimal(0)
    for row in payment.allocations:
        inv = invoices_by_id.get(row.invoice_id)
        if inv is None or not inv.is_posted or inv.is_cancelled:
            raise AccountingError("Invoice not posted, absent or cancelled")
        if inv.partner_id != payment.partner_id or inv.currency != payment.currency:
            raise AccountingError("Invoice counterparty or currency mismatch")
        if payment.occurred_on < inv.issued_on:
            raise AccountingError("Payment before invoice issue date")
        amount = _validated_decimal(row.amount, precision=unit)
        due = inv.outstanding
        if not isinstance(due, Decimal) or not due.is_finite() or due < 0:
            raise AccountingError("Invalid outstanding amount")
        if amount > due:
            raise AccountingError("Payment allocation exceeds invoice balance")
        allocated += amount
    if allocated != total:
        raise AccountingError("Payment amount must equal sum of its allocations")
    bank = chart.get(accounts.bank_account_id)
    account_id = (accounts.receivable_id if payment.direction is PaymentDirection.CUSTOMER_RECEIPT
                  else accounts.payable_id)
    control = chart.get(account_id)
    expected = (AccountKind.ASSET if payment.direction is PaymentDirection.CUSTOMER_RECEIPT
                else AccountKind.LIABILITY)
    if (
        bank is None or bank.tenant_id != payment.tenant_id or
        bank.kind is not AccountKind.ASSET or not bank.active or not bank.posting_allowed or
        control is None or control.tenant_id != payment.tenant_id or
        control.kind is not expected or not control.active or not control.posting_allowed or
        accounts.bank_account_id == account_id
    ):
        raise AccountingError("Invalid bank/receivables/payables account configuration")
    if payment.direction is PaymentDirection.CUSTOMER_RECEIPT:
        lines = (JournalLine(accounts.bank_account_id, debit=total),
                 JournalLine(account_id, credit=total))
    else:
        lines = (JournalLine(account_id, debit=total),
                 JournalLine(accounts.bank_account_id, credit=total))
    journal = JournalEntry(
        payment.id, payment.tenant_id,
        f"{payment.direction.value}:{payment.bank_reference}",
        payment.occurred_on, payment.currency, f"payment:{payment.id}",
        lines,
    )
    validate_entry(journal, chart=chart, period=period, minor_units=minor_units)
    return PaymentProposal(payment,journal,allocated)
