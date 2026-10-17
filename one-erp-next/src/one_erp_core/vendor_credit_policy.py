"""Fail-closed, tax-free billed vendor-return preflight for ONE ERP Next.

This policy is NOT a physical return, GL posting, vendor credit issuance, or
authenticated API. All evidence must come from locked authoritative rows in
the physically isolated tenant DB. A live handler must recheck atomically.
"""
from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID
from .accounting import AccountingError
from .invoicing import DocumentType, InvoiceDocument, TaxCategory


@dataclass(frozen=True, slots=True)
class BilledVendorReturnSource:
    tenant_id: UUID
    return_job_id: UUID
    source_receipt_id: UUID
    original_bill_id: UUID
    supplier_id: str
    currency: str
    return_status: str
    requested_quantity: Decimal
    receipt_quantity: Decimal
    receipt_cost: Decimal
    previously_returned_from_receipt: Decimal
    po_line_billed_quantity: Decimal
    previously_billed_returned_po_line: Decimal
    original_bill_line_quantity: Decimal
    previously_credited_bill_line_quantity: Decimal
    available_unreserved_quantity: Decimal
    position_quantity: Decimal
    position_book_value: Decimal
    position_currency: str
    original_unit_price: Decimal
    original_tax_category: TaxCategory
    original_tax_rate: Decimal
    original_tax_code: str
    original_input_tax_recoverable: bool
    original_bill_issued: bool
    original_receipt_valued: bool
    source_bill_line_allocated_to_po: bool


@dataclass(frozen=True, slots=True)
class BilledVendorCreditProposal:
    tenant_id: UUID
    return_job_id: UUID
    original_bill_id: UUID
    quantity: Decimal
    debit_accounts_payable: Decimal
    credit_inventory: Decimal
    inventory_quantity_after: Decimal
    inventory_value_after: Decimal
    currency: str


def _nonnegative(value: Decimal, name: str, quantum: Decimal | None = None) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise AccountingError(f"{name}: finite Decimal required")
    if value < 0 or (quantum is not None and value != value.quantize(quantum)):
        raise AccountingError(f"{name}: invalid sign or precision")
    return value


def propose_billed_vendor_credit(
    *, source: BilledVendorReturnSource, credit: InvoiceDocument,
    minor_units: int = 2
) -> BilledVendorCreditProposal:
    """Preview Dr AP / Cr inventory without any state mutation.

    Supports only a single billed source line, zero tax/discount, same
    source cost and book weighted-average cost. No silent cost variances,
    VAT, FX, payment, supplier confirmation, or ZATCA e-document.
    """
    if minor_units not in (0, 2, 3):
        raise AccountingError("Unsupported money precision")
    unit = Decimal(1).scaleb(-minor_units)
    q_unit = Decimal("0.001")
    if not isinstance(source, BilledVendorReturnSource) or not isinstance(credit, InvoiceDocument):
        raise AccountingError("Verified source and credit required")
    if not all(isinstance(x, UUID) for x in (
        source.tenant_id, source.return_job_id, source.source_receipt_id,
        source.original_bill_id, credit.tenant_id, credit.id
    )):
        raise AccountingError("Original immutable source IDs required")
    if (credit.kind is not DocumentType.VENDOR_CREDIT
        or credit.tenant_id != source.tenant_id
        or credit.source_invoice_id != source.original_bill_id
        or credit.partner_id != source.supplier_id
        or credit.currency != source.currency
        or source.position_currency != source.currency
        or source.return_status != "requested"
        or not source.original_bill_issued
        or not source.original_receipt_valued
        or not source.source_bill_line_allocated_to_po
        or len(credit.lines) != 1):
        raise AccountingError("Billed supplier-return source linkage refused")
    line = credit.lines[0]
    if (line.tax_category not in (TaxCategory.OUT_OF_SCOPE, TaxCategory.ZERO_RATED)
        or line.tax_category != source.original_tax_category
        or line.tax_code != source.original_tax_code
        or line.input_tax_recoverable or source.original_input_tax_recoverable):
        raise AccountingError("Tax-free source snapshot required")
    qty = _nonnegative(line.quantity, "Credit quantity", q_unit)
    requested = _nonnegative(source.requested_quantity, "Return quantity", q_unit)
    if qty <= 0 or qty != requested:
        raise AccountingError("Vendor return must credit its entire requested quantity")
    for name, value in (
        ("Original receipt quantity", source.receipt_quantity),
        ("Previously returned receipt units", source.previously_returned_from_receipt),
        ("Billed PO units", source.po_line_billed_quantity),
        ("Previously billed returned PO units", source.previously_billed_returned_po_line),
        ("Original bill line units", source.original_bill_line_quantity),
        ("Previously credited bill units", source.previously_credited_bill_line_quantity),
        ("Available units", source.available_unreserved_quantity),
        ("Valued units", source.position_quantity),
    ):
        _nonnegative(value, name, q_unit)
    for name, value in (
        ("Receipt value", source.receipt_cost),
        ("Book value", source.position_book_value),
        ("Original unit price", source.original_unit_price),
        ("Credit unit price", line.unit_price),
        ("Credit discount", line.discount_amount),
        ("Credit tax rate", line.tax_rate),
        ("Original tax rate", source.original_tax_rate),
    ):
        _nonnegative(value, name)
    if (source.receipt_quantity <= 0 or source.receipt_cost <= 0
        or source.position_quantity <= 0 or source.position_book_value <= 0
        or source.original_unit_price <= 0
        or source.original_unit_price != line.unit_price
        or source.original_tax_rate != 0 or line.tax_rate != 0
        or line.discount_amount != 0):
        raise AccountingError("Original price/cost or tax policy mismatch")
    if (source.previously_returned_from_receipt + qty > source.receipt_quantity
        or source.previously_billed_returned_po_line + qty > source.po_line_billed_quantity
        or source.previously_credited_bill_line_quantity + qty > source.original_bill_line_quantity
        or qty > source.available_unreserved_quantity
        or qty > source.position_quantity):
        raise AccountingError("Return exceeds unused source, bill or available inventory")
    amount = (line.unit_price * qty).quantize(unit, rounding=ROUND_HALF_UP)
    source_cost = (source.receipt_cost * qty / source.receipt_quantity).quantize(
        unit, rounding=ROUND_HALF_UP)
    book_cost = (source.position_book_value * qty / source.position_quantity).quantize(
        unit, rounding=ROUND_HALF_UP)
    if qty == source.position_quantity:
        book_cost = source.position_book_value
    remaining_qty = source.position_quantity - qty
    remaining_value = source.position_book_value - book_cost
    if (amount <= 0 or amount != source_cost or amount != book_cost
        or remaining_value < 0
        or (remaining_qty == 0 and remaining_value != 0)
        or (remaining_qty > 0 and remaining_value <= 0)):
        raise AccountingError("Source price/book value variance needs explicit approved treatment")
    return BilledVendorCreditProposal(
        tenant_id=source.tenant_id, return_job_id=source.return_job_id,
        original_bill_id=source.original_bill_id, quantity=qty,
        debit_accounts_payable=amount, credit_inventory=book_cost,
        inventory_quantity_after=remaining_qty,
        inventory_value_after=remaining_value, currency=source.currency)
