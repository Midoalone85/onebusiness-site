"""Source-traceable financial summaries derived only from posted journals."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Mapping, Sequence
from uuid import UUID

from .accounting import Account, AccountKind, AccountingError, EntryStatus, JournalEntry


@dataclass(frozen=True, slots=True)
class TrialBalanceRow:
    account_code: str
    account_name: str
    kind: AccountKind
    debit: Decimal
    credit: Decimal
    closing_debit: Decimal
    closing_credit: Decimal
    source_entry_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class TrialBalance:
    tenant_id: UUID
    as_of: date
    currency: str
    rows: tuple[TrialBalanceRow, ...]
    total_debit: Decimal
    total_credit: Decimal
    balanced: bool


def trial_balance(*, tenant_id: UUID, as_of: date, currency: str,
                  chart: Mapping[str, Account],
                  entries: Sequence[JournalEntry]) -> TrialBalance:
    """Show gross movements and net balances up to as_of (all prior dates).

    Fiscal openings/closings and external reconciliations are separate stages.
    Reject mixed-tenant inputs rather than silently risk data leakage.
    """
    if any(e.tenant_id != tenant_id for e in entries):
        raise AccountingError("Cross-tenant journal data supplied to report")
    if any(a.tenant_id != tenant_id for a in chart.values()):
        raise AccountingError("Cross-tenant chart data supplied to report")
    posted = [e for e in entries if e.status == EntryStatus.POSTED and e.occurred_on <= as_of]
    if any(e.currency != currency for e in posted):
        raise AccountingError("Currency conversion requires an approved FX policy")
    values: dict[str, tuple[Decimal, Decimal, list[UUID]]] = {}
    for e in posted:
        for line in e.lines:
            if line.account_id not in chart:
                raise AccountingError("Report references missing account")
            d, c, ids = values.setdefault(line.account_id, (Decimal(0), Decimal(0), []))
            ids.append(e.id)
            values[line.account_id] = (d + line.debit, c + line.credit, ids)
    rows = []
    for key, (d, c, ids) in sorted(values.items(), key=lambda item: chart[item[0]].code):
        account = chart[key]
        delta = d - c
        rows.append(TrialBalanceRow(account.code, account.name, account.kind, d, c,
                                    max(delta, Decimal(0)), max(-delta, Decimal(0)),
                                    tuple(dict.fromkeys(ids))))
    total_d = sum((r.debit for r in rows), Decimal(0))
    total_c = sum((r.credit for r in rows), Decimal(0))
    return TrialBalance(tenant_id, as_of, currency, tuple(rows), total_d, total_c, total_d == total_c)


@dataclass(frozen=True, slots=True)
class ProfitAndLoss:
    tenant_id: UUID
    from_date: date
    through_date: date
    currency: str
    revenue: Decimal
    expense: Decimal
    net_profit: Decimal
    source_entry_ids: tuple[UUID, ...]


def profit_and_loss(*, tenant_id: UUID, from_date: date, through_date: date,
                    currency: str, chart: Mapping[str, Account],
                    entries: Sequence[JournalEntry]) -> ProfitAndLoss:
    """Accrual P&L on an inclusive date range; income taxes excluded."""
    if from_date > through_date:
        raise AccountingError("Invalid P&L date range")
    if any(e.tenant_id != tenant_id for e in entries) or any(a.tenant_id != tenant_id for a in chart.values()):
        raise AccountingError("Cross-tenant data supplied to report")
    revenue = expense = Decimal(0)
    used: list[UUID] = []
    for entry in entries:
        if entry.status != EntryStatus.POSTED or not from_date <= entry.occurred_on <= through_date:
            continue
        if entry.currency != currency:
            raise AccountingError("Currency conversion requires an approved FX policy")
        for line in entry.lines:
            account = chart.get(line.account_id)
            if account is None:
                raise AccountingError("Report references missing account")
            if account.kind == AccountKind.REVENUE:
                revenue += line.credit - line.debit
                used.append(entry.id)
            elif account.kind == AccountKind.EXPENSE:
                expense += line.debit - line.credit
                used.append(entry.id)
    return ProfitAndLoss(tenant_id, from_date, through_date, currency,
                         revenue, expense, revenue - expense, tuple(dict.fromkeys(used)))
