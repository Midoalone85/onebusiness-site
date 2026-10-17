"""Conservative one-to-one candidate matching for imported bank statements.

This module produces ONLY immutable MANUAL REVIEW suggestions. It never
authenticates a bank/PSP, issues settlement, releases a refund, posts a ledger
entry, or marks a payment as bank confirmed. Imported statements may be fake.
A separate signed provider-ingestion service, independent reviewer and
transactional uniqueness register are mandatory before any actual settlement.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Sequence
from uuid import UUID

from .accounting import AccountingError
from .reconciliation import (
    BankMovement,LedgerMovement,MatchCandidate,propose_bank_matches,
)


@dataclass(frozen=True,slots=True)
class BankBatchCandidate:
    bank_line_id: str
    matching_ledger_ids: tuple[str,...]
    best_candidate: MatchCandidate | None
    outcome: str
    requires_independent_review: bool = True


@dataclass(frozen=True,slots=True)
class BankBatchProposal:
    tenant_id: UUID
    items: tuple[BankBatchCandidate,...]
    statement_evidence: str = "import_unverified"
    bank_settlement_verified: bool = False
    writes_performed: bool = False


def propose_bank_batch(
    tenant_id: UUID, *,
    bank_lines: Sequence[BankMovement],
    ledger_rows: Sequence[LedgerMovement],
    day_tolerance: int = 5,
) -> BankBatchProposal:
    """Rank batch candidates and detect cross-line reuse of a ledger entry.

    A unique match is still only a suggestion until independent bank source
    authentication and human approval. Conflicts are never silently resolved
    by score, and no split/partial amount allocation is authorized here.
    """
    if not isinstance(tenant_id,UUID) or tenant_id.int==0:
        raise AccountingError("Verified tenant context required")
    if (not isinstance(bank_lines,(tuple,list)) or
        not isinstance(ledger_rows,(tuple,list)) or
        len(bank_lines)>200 or len(ledger_rows)>2000):
        raise AccountingError("Bounded bank matching batch required")
    if (type(day_tolerance) is not int or
        not 0<=day_tolerance<=31):
        raise AccountingError("Reviewed matching window required")
    def decimal_amount(value):
        return (isinstance(value,Decimal) and value.is_finite() and
                value!=0 and value==value.quantize(Decimal("0.001")))
    for obj in bank_lines:
        if (not isinstance(obj,BankMovement) or obj.tenant_id!=tenant_id or
            type(obj.id) is not str or not 1<=len(obj.id)<=160 or not obj.id.strip() or
            type(obj.currency) is not str or len(obj.currency)!=3 or
            not obj.currency.isascii() or not obj.currency.isalpha() or
            not obj.currency.isupper() or not decimal_amount(obj.amount) or
            type(obj.value_date) is not date or type(obj.reference) is not str):
            raise AccountingError("Invalid or cross-company bank statement row")
    for obj in ledger_rows:
        if (not isinstance(obj,LedgerMovement) or obj.tenant_id!=tenant_id or
            type(obj.id) is not str or not 1<=len(obj.id)<=160 or not obj.id.strip() or
            type(obj.currency) is not str or len(obj.currency)!=3 or
            not obj.currency.isascii() or not obj.currency.isalpha() or
            not obj.currency.isupper() or not decimal_amount(obj.amount) or
            type(obj.occurred_on) is not date or type(obj.reference) is not str or
            type(obj.is_posted) is not bool or type(obj.is_reconciled) is not bool):
            raise AccountingError("Invalid or cross-company ledger row")
    if (len({x.id for x in bank_lines})!=len(bank_lines) or
        len({x.id for x in ledger_rows})!=len(ledger_rows)):
        raise AccountingError("Duplicate source record identifiers")
    proposals=[
        propose_bank_matches(line,ledger=ledger_rows,
                             day_tolerance=day_tolerance,max_candidates=25)
        for line in bank_lines
    ]
    # Scan every eligible ledger ID, not only the top 25 returned by each
    # bank suggestion. This also catches collision with ambiguous bank rows.
    claimed:dict[str,set[str]]={}
    for bank in bank_lines:
        for ledger in ledger_rows:
            if (ledger.is_posted and not ledger.is_reconciled and
                ledger.amount==bank.amount and ledger.currency==bank.currency and
                abs((ledger.occurred_on-bank.value_date).days)<=day_tolerance):
                claimed.setdefault(ledger.id,set()).add(bank.id)
    results=[]
    for bank,prop in zip(bank_lines,proposals):
        ids=tuple(c.ledger_id for c in prop.candidates)
        best=prop.candidates[0] if prop.candidates else None
        if not ids:
            status="no_candidate"
        elif prop.ambiguous or len(ids)>1:
            status="ambiguous_multiple_ledger_candidates"
        elif len(claimed.get(ids[0],()))>1:
            status="collision_same_ledger_proposed_for_multiple_bank_lines"
        else:
            status="unique_candidate_requires_independent_review"
        results.append(BankBatchCandidate(
            bank_line_id=bank.id,matching_ledger_ids=ids,
            best_candidate=best,outcome=status,
        ))
    return BankBatchProposal(tenant_id=tenant_id,items=tuple(results))
