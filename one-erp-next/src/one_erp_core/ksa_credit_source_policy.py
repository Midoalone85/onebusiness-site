"""KSA standard-rated goods-return credit evidence gate, NOT ZATCA issuance.

This deliberately narrow pure validator does not generate EN16931/UBL XML,
assign ICV/PIH, sign, add QR, authenticate, clear, report, archive, or
adjust a filed VAT return. No "compliant" or "ready to send" verdict.
Only accepts independently authenticated, immutable source snapshots.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from .accounting import AccountingError
from .invoicing import (
    DocumentType, InvoiceDocument, TaxCategory, calculate_invoice,
)


@dataclass(frozen=True, slots=True)
class KSASourceNoteEvidence:
    original_invoice_id: UUID
    original_invoice_number: str
    source_registered: bool
    original_fatoora_route: str
    original_zatca_state: str
    seller_vat_number: str
    buyer_legal_identity_confirmed: bool
    accepted_return_id: UUID
    accepted_return_quantity: Decimal
    earlier_credited_source_quantity: Decimal
    approved_vat_code: str
    reason: str
    reason_detail: str


@dataclass(frozen=True, slots=True)
class KSACreditEvidenceAssessment:
    original_invoice_number: str
    original_invoice_id: UUID
    accepted_return_id: UUID
    credit_id: UUID
    expected_document_type: str
    required_zatca_route: str
    base_amount_sar: Decimal
    output_vat_reversal_sar: Decimal
    total_credit_sar: Decimal


def assess_ksa_standard_goods_credit_source(
    *, original: InvoiceDocument, credit: InvoiceDocument,
    evidence: KSASourceNoteEvidence,
) -> KSACreditEvidenceAssessment:
    """Validate original invoice and return snapshot for FUTURE legal review.

    Bounded scope: one line, no discounts, SAR only, standard 15% VAT.
    Result is a *source-consistency assessment*, not tax approval. Neither
    paid customer refunds nor QR/hash/signing can be inferred from it.
    """
    if (not isinstance(original, InvoiceDocument)
        or not isinstance(credit, InvoiceDocument)
        or not isinstance(evidence, KSASourceNoteEvidence)
        or not isinstance(original.id, UUID)
        or not isinstance(credit.id, UUID)
        or not isinstance(evidence.accepted_return_id, UUID)
        or not isinstance(evidence.original_invoice_id, UUID)):
        raise AccountingError("Trusted immutable source and return identities required")
    if (original.kind is not DocumentType.CUSTOMER_INVOICE
        or credit.kind is not DocumentType.CUSTOMER_CREDIT
        or original.id != evidence.original_invoice_id
        or credit.source_invoice_id != original.id
        or original.tenant_id != credit.tenant_id
        or original.partner_id != credit.partner_id
        or original.currency != "SAR" or credit.currency != "SAR"
        or credit.issued_on < original.issued_on
        or not evidence.source_registered
        or evidence.original_invoice_number != original.number
        or evidence.reason != "goods_return"
        or not isinstance(evidence.reason_detail, str)
        or len(evidence.reason_detail.strip()) < 5
        or not evidence.buyer_legal_identity_confirmed
        or evidence.original_fatoora_route not in ("standard", "simplified")
        or evidence.original_zatca_state not in ("cleared", "reported")
        or (evidence.original_fatoora_route == "standard"
            and evidence.original_zatca_state != "cleared")
        or (evidence.original_fatoora_route == "simplified"
            and evidence.original_zatca_state != "reported")
        or not isinstance(evidence.seller_vat_number, str)
        or len(evidence.seller_vat_number) != 15
        or not evidence.seller_vat_number.isascii()
        or not evidence.seller_vat_number.isdecimal()
        or evidence.seller_vat_number == "0" * 15
        or len(original.lines) != 1 or len(credit.lines) != 1
        or not credit.number.strip() or credit.number == original.number
        or not evidence.approved_vat_code.strip()):
        raise AccountingError("Saudi credit source legal evidence not sufficient")
    o, c = original.lines[0], credit.lines[0]
    for name, value in (
        ("accepted qty", evidence.accepted_return_quantity),
        ("prior credit qty", evidence.earlier_credited_source_quantity),
    ):
        if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
            raise AccountingError(f"Invalid {name}")
        if value != value.quantize(Decimal("0.001")):
            raise AccountingError(f"Invalid {name} precision")
    if (not isinstance(o.quantity, Decimal)
        or not isinstance(c.quantity, Decimal)
        or not o.quantity.is_finite() or not c.quantity.is_finite()
        or o.quantity <= 0 or c.quantity <= 0
        or o.quantity != o.quantity.quantize(Decimal("0.001"))
        or c.quantity != c.quantity.quantize(Decimal("0.001"))
        or c.quantity != evidence.accepted_return_quantity
        or c.quantity + evidence.earlier_credited_source_quantity > o.quantity
        or o.tax_category is not TaxCategory.STANDARD
        or c.tax_category is not TaxCategory.STANDARD
        or o.tax_rate != Decimal("0.15")
        or c.tax_rate != o.tax_rate
        or o.tax_code != evidence.approved_vat_code
        or c.tax_code != o.tax_code
        or c.account_id != o.account_id
        or c.unit_price != o.unit_price
        or o.discount_amount != 0 or c.discount_amount != 0
        or o.input_tax_recoverable or c.input_tax_recoverable):
        raise AccountingError("VAT/price/quantity mismatch requires tax review")
    org_totals = calculate_invoice(original, minor_units=2)
    note_totals = calculate_invoice(credit, minor_units=2)
    if note_totals.gross > org_totals.gross:
        raise AccountingError("Credit greater than original gross invoice")
    return KSACreditEvidenceAssessment(
        original_invoice_number=original.number,
        original_invoice_id=original.id,
        accepted_return_id=evidence.accepted_return_id,
        credit_id=credit.id,
        expected_document_type="381",
        required_zatca_route=evidence.original_fatoora_route,
        base_amount_sar=note_totals.net,
        output_vat_reversal_sar=note_totals.tax,
        total_credit_sar=note_totals.gross,
    )
