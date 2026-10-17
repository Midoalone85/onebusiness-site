"""Read-only reconciliation diagnostics; never authorize or post cash."""
from decimal import Decimal, InvalidOperation

def bank_candidate_mismatches(approved_instruction, candidate):
    """Return safe field-level mismatch codes for a preapproved refund instruction.

    This helper is diagnostic only, not a payment authorization. Do not use its
    output to skip any SQL-side authorization or reference-matching checks.
    """
    if approved_instruction is None:
        return ["approved_instruction_missing"]
    if len(approved_instruction) != 8:
        return ["unexpected_approved_instruction_shape"]
    _, _, _, currency, amount, beneficiary, independent, status = approved_instruction
    if not isinstance(candidate, dict):
        return ["invalid_candidate"]
    mismatches = []
    if status != "approved_not_disbursed":
        mismatches.append("not_approved_unpaid")
    if candidate.get("currency") != currency:
        mismatches.append("currency")
    try:
        supplied = Decimal(str(candidate.get("amount")))
        expected = Decimal(str(amount))
        if not supplied.is_finite() or not expected.is_finite() or supplied != expected:
            mismatches.append("amount")
    except (InvalidOperation, ValueError, TypeError):
        mismatches.append("amount")
    if candidate.get("beneficiary") != beneficiary:
        mismatches.append("beneficiary")
    if not independent:
        mismatches.append("missing_independent_review")
    # Reference validation must stay in the database's source-backed policy.
    return mismatches
