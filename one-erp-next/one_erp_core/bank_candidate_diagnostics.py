"""Read-only diagnostics for refund bank reconciliation, no secrets or payouts.

Run from tests with a connected, disposable PostgreSQL CI database only.
This script deliberately does not attempt to bypass maker-checker.
"""
from decimal import Decimal

def bank_candidate_mismatches(approved_instruction, candidate):
    """Compare known approval fields without mutating data.

    approved_instruction is the tuple returned by one_refund_approved_instruction_preview:
      (order_id, invoice_id, partner_id, currency, amount,
       beneficiary_evidence, independent_review_evidence, state)
    candidate contains keys currency, amount, beneficiary, payment_reference.
    Never interpret a bank candidate as proof of cash payment.
    """
    if approved_instruction is None:
        return ["approved_instruction_missing"]
    if len(approved_instruction) != 8:
        return ["unexpected_approved_instruction_shape"]
    _, _, _, currency, amount, beneficiary, independent, status = approved_instruction
    mismatches = []
    if status != "approved_not_disbursed":
        mismatches.append("not_approved_unpaid")
    if candidate.get("currency") != currency:
        mismatches.append("currency")
    if Decimal(str(candidate.get("amount"))) != Decimal(str(amount)):
        mismatches.append("amount")
    if candidate.get("beneficiary") != beneficiary:
        mismatches.append("beneficiary")
    if not independent:
        mismatches.append("missing_independent_review")
    # Payment reference is deliberately not equated to order UUID here:
    # bank reference requirements must be read from the actual SQL contract.
    return mismatches
