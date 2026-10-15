"""ONE ERP Next: deterministic invoice-to-journal draft policies.

No government e-invoice issuance, legal tax validation, bank settlement, or
automatic posting is performed here. Tax rule/rate and recoverability MUST
come from a reviewed country pack, not arbitrary end-user input, in production.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from uuid import UUID

from .accounting import (
    Account, AccountKind, AccountingError, JournalEntry, JournalLine,
    validate_entry, AccountingPeriod,
)


class DocumentType(str, Enum):
    CUSTOMER_INVOICE = "customer_invoice"
    VENDOR_BILL = "vendor_bill"
    CUSTOMER_CREDIT = "customer_credit"
    VENDOR_CREDIT = "vendor_credit"


class TaxCategory(str, Enum):
    STANDARD = "standard"
    ZERO_RATED = "zero_rated"
    EXEMPT = "exempt"
    OUT_OF_SCOPE = "out_of_scope"


@dataclass(frozen=True, slots=True)
class InvoiceLine:
    description: str
    account_id: str
    quantity: Decimal
    unit_price: Decimal
    discount_amount: Decimal = Decimal("0")
    tax_category: TaxCategory = TaxCategory.OUT_OF_SCOPE
    tax_rate: Decimal = Decimal("0")
    tax_code: str = "NONE"
    input_tax_recoverable: bool = False


@dataclass(frozen=True, slots=True)
class InvoiceDocument:
    id: UUID
    tenant_id: UUID
    number: str
    partner_id: str
    kind: DocumentType
    issued_on: date
    due_on: date
    currency: str
    lines: tuple[InvoiceLine, ...]
    source_invoice_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class InvoiceLedgerAccounts:
    receivable_id: str
    payable_id: str
    output_vat_id: str
    input_vat_id: str


@dataclass(frozen=True, slots=True)
class InvoiceLineTotals:
    base: Decimal
    tax: Decimal
    total: Decimal


@dataclass(frozen=True, slots=True)
class InvoiceTotals:
    net: Decimal
    tax: Decimal
    gross: Decimal
    details: tuple[InvoiceLineTotals, ...]


@dataclass(frozen=True, slots=True)
class InvoicePostingProposal:
    document: InvoiceDocument
    totals: InvoiceTotals
    journal: JournalEntry


def _dec(value: Decimal, name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise AccountingError(f"{name} must be finite Decimal, not float")
    return value


def _minor(minor_units: int) -> Decimal:
    if minor_units not in (0, 2, 3):
        raise AccountingError("Unsupported currency minor units")
    return Decimal(1).scaleb(-minor_units)


def _kind_account(chart: dict[str, Account], key: str, kind: AccountKind,
                  tenant_id: UUID) -> Account:
    a = chart.get(key)
    if a is None or a.tenant_id != tenant_id or a.kind is not kind:
        raise AccountingError(f"Account {key} is missing, cross-tenant, or wrong category")
    if not a.active or not a.posting_allowed:
        raise AccountingError("Inactive/non-posting account")
    return a


def calculate_invoice(
    document: InvoiceDocument, *, minor_units: int = 2
) -> InvoiceTotals:
    """Compute lines under an explicitly configured line-level half-up rule.

    This is illustrative; each country pack must approve its tax rounding,
    discounts, tax category and input VAT recoverability before live issuance.
    """
    unit = _minor(minor_units)
    if not document.number.strip() or not document.partner_id.strip():
        raise AccountingError("Invoice number and counterparty are required")
    if not document.currency.isascii() or len(document.currency) != 3 or not document.currency.isalpha() or document.currency != document.currency.upper():
        raise AccountingError("Currency code must be ISO-style uppercase")
    if document.due_on < document.issued_on:
        raise AccountingError("Due date cannot be before issue date")
    if document.source_invoice_id == document.id:
        raise AccountingError("A credit note cannot refer to itself")
    is_credit = document.kind in (DocumentType.CUSTOMER_CREDIT, DocumentType.VENDOR_CREDIT)
    if is_credit != (document.source_invoice_id is not None):
        raise AccountingError("Credit notes require a source invoice, regular invoices do not")
    if not document.lines:
        raise AccountingError("At least one invoice line is required")
    total_net = total_tax = Decimal("0")
    details = []
    for line in document.lines:
        if not line.description.strip() or not line.account_id.strip():
            raise AccountingError("Line description and GL account required")
        qty = _dec(line.quantity, "quantity")
        price = _dec(line.unit_price, "price")
        discount = _dec(line.discount_amount, "discount")
        rate = _dec(line.tax_rate, "tax rate")
        if qty <= 0 or price < 0 or discount < 0:
            raise AccountingError("Negative or zero quantities / negative amounts forbidden")
        # Price and discount are monetary amounts in the invoice currency.
        if price != price.quantize(unit) or discount != discount.quantize(unit):
            raise AccountingError("Unit price and discount must fit currency precision")
        exact_subtotal = qty * price
        if discount > exact_subtotal:
            raise AccountingError("Discount exceeds line value")
        if rate < 0 or rate > 1:
            raise AccountingError("Tax rate must be between zero and one")
        if line.tax_category is TaxCategory.STANDARD:
            if rate <= 0 or not line.tax_code.strip() or line.tax_code == "NONE":
                raise AccountingError("Standard tax requires positive rate and tax code")
        elif rate != 0:
            raise AccountingError("Zero/exempt/out-of-scope lines must have zero tax rate")
        if document.kind in (DocumentType.CUSTOMER_INVOICE, DocumentType.CUSTOMER_CREDIT) and line.input_tax_recoverable:
            raise AccountingError("Input tax recoverability only applies to vendor documents")
        base = (exact_subtotal - discount).quantize(unit, rounding=ROUND_HALF_UP)
        tax = (base * rate).quantize(unit, rounding=ROUND_HALF_UP)
        details.append(InvoiceLineTotals(base, tax, base + tax))
        total_net += base
        total_tax += tax
    if total_net <= 0:
        raise AccountingError("Non-positive invoice net amount")
    return InvoiceTotals(total_net, total_tax, total_net + total_tax, tuple(details))


def propose_invoice_journal(
    document: InvoiceDocument,
    *,
    accounts: InvoiceLedgerAccounts,
    chart: dict[str, Account],
    period: AccountingPeriod,
    minor_units: int = 2,
) -> InvoicePostingProposal:
    """Return a balanced *draft* journal; nothing is saved or posted.

    Debit and credit are produced from source invoice and tax snapshot.
    Subledger, approval, currency conversion and country-tax validation are
    separate controlled workflows.
    """
    totals = calculate_invoice(document, minor_units=minor_units)
    is_customer = document.kind in (DocumentType.CUSTOMER_INVOICE, DocumentType.CUSTOMER_CREDIT)
    is_credit = document.kind in (DocumentType.CUSTOMER_CREDIT, DocumentType.VENDOR_CREDIT)
    control = (accounts.receivable_id if is_customer else accounts.payable_id)
    _kind_account(chart, control, AccountKind.ASSET if is_customer else AccountKind.LIABILITY, document.tenant_id)

    lines = []
    # A credit note inverts each accounting side. Not the same operation
    # as a literal journal reversal, which is handled by reversals.py.
    def append_side(key: str, amount: Decimal, debit: bool) -> None:
        if amount == 0:
            return
        actual_debit = not debit if is_credit else debit
        lines.append(JournalLine(
            key, debit=amount if actual_debit else Decimal(0),
            credit=amount if not actual_debit else Decimal(0),
        ))

    for line, detail in zip(document.lines, totals.details):
        _kind_account(
            chart, line.account_id,
            AccountKind.REVENUE if is_customer else AccountKind.EXPENSE,
            document.tenant_id,
        )
        if is_customer:
            append_side(line.account_id, detail.base, debit=False)
        else:
            # Non-recoverable tax is part of the acquisition cost/expense.
            expense_value = detail.base + (Decimal(0) if line.input_tax_recoverable else detail.tax)
            append_side(line.account_id, expense_value, debit=True)
        if detail.tax:
            if is_customer:
                _kind_account(chart, accounts.output_vat_id, AccountKind.LIABILITY, document.tenant_id)
                append_side(accounts.output_vat_id, detail.tax, debit=False)
            elif line.input_tax_recoverable:
                _kind_account(chart, accounts.input_vat_id, AccountKind.ASSET, document.tenant_id)
                append_side(accounts.input_vat_id, detail.tax, debit=True)
    append_side(control, totals.gross, debit=is_customer)
    if len(lines) < 2:
        raise AccountingError("Cannot form balanced invoice journal")
    journal = JournalEntry(
        id=document.id, tenant_id=document.tenant_id,
        reference=f"{document.kind.value}:{document.number}",
        occurred_on=document.issued_on, currency=document.currency,
        idempotency_key=f"invoice:{document.id}",
        lines=tuple(lines),
    )
    validate_entry(journal, chart=chart, period=period, minor_units=minor_units)
    return InvoicePostingProposal(document, totals, journal)
