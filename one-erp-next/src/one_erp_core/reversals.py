"""Policy for traceable reversing entries; posted journals are never edited."""
from __future__ import annotations

from datetime import date
from uuid import UUID, uuid4
from typing import Sequence

from .accounting import AccountingError, EntryStatus, JournalEntry, JournalLine


def make_reversal(
    source: JournalEntry, *,
    reversal_on: date,
    reason: str,
    existing_reversals: Sequence[JournalEntry] = (),
    reversal_id: UUID | None = None,
) -> JournalEntry:
    """Create an exact draft reversal on a new date, in the same tenant/currency.

    Database UNIQUE(tenant_id,reversal_of), status and line checks remain
    authoritative. Caller must choose an open accounting period on posting.
    """
    if source.status is not EntryStatus.POSTED:
        raise AccountingError("Only posted entries can be reversed")
    if source.reversal_of is not None:
        raise AccountingError("Reversals of reversing entries are unsupported")
    if reversal_on < source.occurred_on:
        raise AccountingError("Reversal cannot predate its original journal")
    if not reason.strip():
        raise AccountingError("Documented reason for reversal is required")
    if any(
        e.tenant_id == source.tenant_id and e.reversal_of == source.id
        for e in existing_reversals
    ):
        raise AccountingError("A reversal already exists for this journal")
    return JournalEntry(
        id=reversal_id or uuid4(),
        tenant_id=source.tenant_id,
        reference=f"REV:{source.reference} | {reason.strip()}",
        occurred_on=reversal_on,
        currency=source.currency,
        idempotency_key=f"reversal:{source.id}",
        lines=tuple(
            JournalLine(account_id=l.account_id, debit=l.credit, credit=l.debit)
            for l in source.lines
        ),
        reversal_of=source.id,
    )
