"""ONE ERP NEXT / 040: candidate bank refund reconciliation, not payment.

Pure policy. The ONLY possible result is a candidate needing verified
bank-provider attestation and manual matching. It does not connect to a
bank, emit a payment file, post a journal or mark a customer as paid.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from .accounting import AccountingError


@dataclass(frozen=True, slots=True)
class ApprovedRefundVoucher:
    tenant_id: UUID
    order_id: UUID
    customer_reference: str
    currency: str
    amount: Decimal
    beneficiary_evidence_ref: str
    approved_on: date
    claim_maker_id: UUID
    order_maker_id: UUID
    approver_id: UUID
    approval_state: str


@dataclass(frozen=True, slots=True)
class ImportedBankDebitCandidate:
    tenant_id: UUID
    bank_line_id: UUID
    source_bank_account_ref: str
    currency: str
    debit_amount: Decimal
    value_date: date
    beneficiary_evidence_ref: str
    payment_reference: str
    statement_import_reference: str


@dataclass(frozen=True, slots=True)
class CandidateRefundMatch:
    tenant_id: UUID
    refund_order_id: UUID
    bank_line_id: UUID
    amount: Decimal
    currency: str
    recommendation: str = "candidate_match_manual_review_required"


def check_refund_bank_candidate(
    voucher: ApprovedRefundVoucher,
    bank_line: ImportedBankDebitCandidate,
) -> CandidateRefundMatch:
    """Refuse cross-tenant, wrong beneficiary, currency, date and amounts.

    A matching user-supplied bank record is NOT proof a bank paid money.
    The caller still must verify import signatures/source authenticity,
    statement ownership, uniqueness, matching permissions and settlement.
    """
    if (not isinstance(voucher, ApprovedRefundVoucher)
        or not isinstance(bank_line, ImportedBankDebitCandidate)
        or not all(isinstance(x, UUID) for x in (
            voucher.tenant_id, voucher.order_id,
            voucher.claim_maker_id, voucher.order_maker_id,
            voucher.approver_id, bank_line.tenant_id, bank_line.bank_line_id
        ))
        or not isinstance(voucher.approved_on, date)
        or not isinstance(bank_line.value_date, date)
        or voucher.tenant_id != bank_line.tenant_id
        or voucher.approval_state != "approved"
        or voucher.claim_maker_id == voucher.approver_id
        or voucher.order_maker_id == voucher.approver_id
        or bank_line.value_date < voucher.approved_on):
        raise AccountingError("Independent approved refund and source date required")
    if (not isinstance(voucher.currency, str)
        or voucher.currency != bank_line.currency
        or len(voucher.currency) != 3 or not voucher.currency.isascii()
        or not voucher.currency.isalpha() or not voucher.currency.isupper()):
        raise AccountingError("Original and bank statement currency mismatch")
    if (not isinstance(voucher.amount, Decimal)
        or not isinstance(bank_line.debit_amount, Decimal)
        or not voucher.amount.is_finite() or not bank_line.debit_amount.is_finite()
        or voucher.amount <= 0 or bank_line.debit_amount != voucher.amount
        or voucher.amount != voucher.amount.quantize(Decimal("0.01"))):
        raise AccountingError("Exact positive payout amount and precision required")
    for value in (
        voucher.customer_reference, voucher.beneficiary_evidence_ref,
        bank_line.source_bank_account_ref, bank_line.beneficiary_evidence_ref,
        bank_line.payment_reference, bank_line.statement_import_reference
    ):
        if not isinstance(value, str) or len(value.strip()) < 8:
            raise AccountingError("Bank reconciliation evidence reference missing")
    if voucher.beneficiary_evidence_ref != bank_line.beneficiary_evidence_ref:
        raise AccountingError("Refund beneficiary evidence mismatch")
    if str(voucher.order_id) not in bank_line.payment_reference:
        raise AccountingError("Approved refund identifier not found on bank candidate")
    return CandidateRefundMatch(
        tenant_id=voucher.tenant_id,
        refund_order_id=voucher.order_id,
        bank_line_id=bank_line.bank_line_id,
        amount=voucher.amount,
        currency=voucher.currency
    )
