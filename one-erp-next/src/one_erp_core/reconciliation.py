"""Read-only cash/bank reconciliation proposals.

Matching is deterministic and conservative. These suggestions NEVER post,
approve, clear, reconcile or modify a journal; a human must review and
approve through a separately audited workflow (not implemented here).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID
from typing import Sequence

from .accounting import AccountingError


@dataclass(frozen=True, slots=True)
class BankMovement:
    id: str
    tenant_id: UUID
    value_date: date
    currency: str
    amount: Decimal  # Debit/credit signed according to ONE ERP bank convention.
    reference: str


@dataclass(frozen=True, slots=True)
class LedgerMovement:
    id: str
    tenant_id: UUID
    occurred_on: date
    currency: str
    amount: Decimal  # Same signed convention as bank amount.
    reference: str
    is_posted: bool = True
    is_reconciled: bool = False


@dataclass(frozen=True, slots=True)
class MatchCandidate:
    ledger_id: str
    score: int
    rationale: str
    days_apart: int


@dataclass(frozen=True, slots=True)
class MatchProposal:
    bank_id: str
    candidates: tuple[MatchCandidate, ...]
    ambiguous: bool
    requires_approval: bool = True


def _check_amount(value: Decimal) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise AccountingError("Reconciliation requires finite Decimal amounts")


def _ref(value: str) -> str:
    return "".join(char for char in value.casefold() if char.isalnum())


def propose_bank_matches(
    bank: BankMovement,
    *,
    ledger: Sequence[LedgerMovement],
    day_tolerance: int = 5,
    max_candidates: int = 5,
) -> MatchProposal:
    """Rank exact-amount candidates within a bounded date tolerance.

    No fuzzy amount matching, cross-currency conversion, or auto-approval.
    All source rows must belong to the same tenant, even if not candidates.
    Multiple plausible matches are treated as ambiguous.
    """
    if day_tolerance < 0 or day_tolerance > 31:
        raise AccountingError("Day tolerance must be between 0 and 31")
    if max_candidates < 1 or max_candidates > 25:
        raise AccountingError("Candidate limit must be between 1 and 25")
    if not bank.id.strip() or len(bank.currency) != 3:
        raise AccountingError("Bank movement identity/currency required")
    _check_amount(bank.amount)
    if bank.amount == 0:
        raise AccountingError("Zero bank movement cannot be reconciled")
    if any(item.tenant_id != bank.tenant_id for item in ledger):
        raise AccountingError("Cross-tenant ledger list rejected")
    if len({item.id for item in ledger}) != len(ledger):
        raise AccountingError("Duplicate ledger movement IDs")
    matches: list[MatchCandidate] = []
    bank_reference = _ref(bank.reference)
    for item in ledger:
        _check_amount(item.amount)
        if not item.is_posted or item.is_reconciled:
            continue
        if item.currency != bank.currency or item.amount != bank.amount:
            continue
        gap = abs((item.occurred_on - bank.value_date).days)
        if gap > day_tolerance:
            continue
        item_ref = _ref(item.reference)
        if bank_reference and item_ref and bank_reference == item_ref:
            reference_score, explanation = 45, "exact normalized reference"
        elif (bank_reference and item_ref and
              (bank_reference in item_ref or item_ref in bank_reference)):
            reference_score, explanation = 25, "partial reference"
        else:
            reference_score, explanation = 0, "amount/date match only"
        score = 50 + reference_score + max(0, 5 - gap)
        matches.append(MatchCandidate(
            item.id, score, f"Exact amount and currency; {explanation}", gap
        ))
    matches.sort(key=lambda c: (-c.score, c.days_apart, c.ledger_id))
    candidates = tuple(matches[:max_candidates])
    # Even if one candidate scores highest, competitors require review.
    return MatchProposal(bank.id, candidates, ambiguous=len(matches) > 1)
