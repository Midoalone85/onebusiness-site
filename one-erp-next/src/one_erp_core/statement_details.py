"""ONE ERP Next: permission-scoped, traceable balance sheet and GL detail.

Pure read models. A trusted authenticated API/DB service MUST supply the actor,
membership snapshot, tenant and complete ledger rows from one consistent SQL
snapshot; neither this module nor client-supplied memberships authenticate.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Mapping, Sequence
from uuid import UUID

from .accounting import Account, AccountKind, AccountingError, EntryStatus, JournalEntry
from .tenancy import Action, Membership, can_access


@dataclass(frozen=True, slots=True)
class StatementAccount:
    account_id: str
    code: str
    name: str
    amount: Decimal
    source_entry_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class BalanceSheet:
    tenant_id: UUID
    as_of: date
    currency: str
    assets: tuple[StatementAccount, ...]
    liabilities: tuple[StatementAccount, ...]
    equity: tuple[StatementAccount, ...]
    total_assets: Decimal
    total_liabilities: Decimal
    contributed_and_retained_equity: Decimal
    unclosed_profit_to_date: Decimal
    total_equity_including_profit: Decimal
    accounting_equation_difference: Decimal
    balanced: bool
    source_entry_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class AccountMovement:
    journal_id: UUID
    reference: str
    occurred_on: date
    debit: Decimal
    credit: Decimal
    running_debit_minus_credit: Decimal


@dataclass(frozen=True, slots=True)
class GeneralLedgerDetail:
    tenant_id: UUID
    account_id: str
    account_code: str
    account_name: str
    from_date: date
    through_date: date
    currency: str
    opening_debit_minus_credit: Decimal
    movements: tuple[AccountMovement, ...]
    total_period_debit: Decimal
    total_period_credit: Decimal
    closing_debit_minus_credit: Decimal
    source_entry_ids: tuple[UUID, ...]


def _posted_source(
    *, tenant_id: UUID, currency: str, as_of: date,
    chart: Mapping[str, Account], entries: Sequence[JournalEntry],
    actor_id: UUID, memberships: tuple[Membership, ...],
) -> tuple[JournalEntry, ...]:
    """Fail closed on permission issues and incomplete/corrupted ledger rows.

    Returns only posted journals through as_of, sorted by date and stable ID.
    Tenant checks happen BEFORE filtering drafts or future-dated entries.
    """
    if not can_access(
        actor_id=actor_id, tenant_id=tenant_id,
        action=Action.READ, memberships=memberships,
    ):
        raise AccountingError("Access to company financial reports denied")
    if len(currency) != 3 or not currency.isascii() or not currency.isalpha() or currency.upper() != currency:
        raise AccountingError("Report currency must be ISO-style uppercase")
    if any(a.tenant_id != tenant_id or a.id != key for key, a in chart.items()):
        raise AccountingError("Cross-tenant or mismatched chart account")
    if len({a.code for a in chart.values()}) != len(chart):
        raise AccountingError("Duplicate account codes in report chart")
    if any(e.tenant_id != tenant_id for e in entries):
        raise AccountingError("Cross-tenant source entry supplied to report")
    result = sorted(
        (entry for entry in entries if entry.status == EntryStatus.POSTED
         and entry.occurred_on <= as_of),
        key=lambda e: (e.occurred_on, str(e.id)),
    )
    if len({e.id for e in result}) != len(result):
        raise AccountingError("Duplicate posted journal supplied to report")
    for entry in result:
        if entry.currency != currency:
            raise AccountingError("Cannot mix currencies without approved FX conversion")
        if len(entry.lines) < 2:
            raise AccountingError("Incomplete posted journal")
        debit = credit = Decimal(0)
        for line in entry.lines:
            if line.account_id not in chart:
                raise AccountingError("Posted journal references missing chart account")
            for value in (line.debit, line.credit):
                if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                    raise AccountingError("Invalid accounting value in report source")
            if (line.debit > 0) == (line.credit > 0):
                raise AccountingError("Journal line needs one positive side")
            debit += line.debit
            credit += line.credit
        if debit <= 0 or debit != credit:
            raise AccountingError("Invalid/unbalanced posted source journal")
    return tuple(result)


def balance_sheet(
    *, tenant_id: UUID, as_of: date, currency: str,
    chart: Mapping[str, Account], entries: Sequence[JournalEntry],
    actor_id: UUID, memberships: tuple[Membership, ...],
) -> BalanceSheet:
    """Assets = Liabilities + Equity + remaining unclosed income.

    Historical period opening/closing journals must exist in source data.
    'Unclosed profit' is the residual of *all* unclosed revenue and expense
    postings up to as_of, not specifically the current fiscal-year P&L.
    """
    source = _posted_source(
        tenant_id=tenant_id, currency=currency, as_of=as_of,
        chart=chart, entries=entries, actor_id=actor_id,
        memberships=memberships,
    )
    amounts: dict[str, Decimal] = {}
    trace: dict[str, list[UUID]] = {}
    for entry in source:
        for line in entry.lines:
            amounts[line.account_id] = amounts.get(line.account_id, Decimal(0)) + line.debit - line.credit
            trace.setdefault(line.account_id, []).append(entry.id)
    buckets: dict[AccountKind, list[StatementAccount]] = {
        AccountKind.ASSET: [], AccountKind.LIABILITY: [], AccountKind.EQUITY: [],
    }
    revenue = expense = Decimal(0)
    for key, amount in sorted(amounts.items(), key=lambda kv:(chart[kv[0]].code, kv[0])):
        account = chart[key]
        if account.kind == AccountKind.REVENUE:
            revenue -= amount
        elif account.kind == AccountKind.EXPENSE:
            expense += amount
        elif account.kind in buckets:
            natural = amount if account.kind == AccountKind.ASSET else -amount
            buckets[account.kind].append(
                StatementAccount(key, account.code, account.name, natural,
                                 tuple(dict.fromkeys(trace[key])))
            )
        else:
            raise AccountingError("Unsupported account category")
    assets = tuple(buckets[AccountKind.ASSET])
    liabilities = tuple(buckets[AccountKind.LIABILITY])
    equity = tuple(buckets[AccountKind.EQUITY])
    total_assets = sum((a.amount for a in assets), Decimal(0))
    total_liabilities = sum((a.amount for a in liabilities), Decimal(0))
    invested = sum((a.amount for a in equity), Decimal(0))
    profit = revenue - expense
    total_equity = invested + profit
    difference = total_assets - total_liabilities - total_equity
    return BalanceSheet(
        tenant_id, as_of, currency, assets, liabilities, equity,
        total_assets, total_liabilities, invested, profit,
        total_equity, difference, difference == 0,
        tuple(e.id for e in source),
    )


def general_ledger_detail(
    *, tenant_id: UUID, account_id: str, from_date: date, through_date: date,
    currency: str, chart: Mapping[str, Account],
    entries: Sequence[JournalEntry], actor_id: UUID,
    memberships: tuple[Membership, ...],
) -> GeneralLedgerDetail:
    """Movement-level drill-down with signed debit-minus-credit running balance."""
    if from_date > through_date:
        raise AccountingError("Invalid general ledger date range")
    source = _posted_source(
        tenant_id=tenant_id, currency=currency, as_of=through_date,
        chart=chart, entries=entries, actor_id=actor_id,
        memberships=memberships,
    )
    account = chart.get(account_id)
    if account is None:
        raise AccountingError("Account does not exist in tenant chart")
    opening = running = Decimal(0)
    total_debit = total_credit = Decimal(0)
    movements: list[AccountMovement] = []
    ids: list[UUID] = []
    for entry in source:
        for line in entry.lines:
            if line.account_id != account_id:
                continue
            if entry.occurred_on < from_date:
                opening += line.debit - line.credit
            else:
                if not movements:
                    running = opening
                running += line.debit - line.credit
                total_debit += line.debit
                total_credit += line.credit
                movements.append(AccountMovement(
                    entry.id, entry.reference, entry.occurred_on,
                    line.debit, line.credit, running,
                ))
                ids.append(entry.id)
    return GeneralLedgerDetail(
        tenant_id, account_id, account.code, account.name,
        from_date, through_date, currency, opening,
        tuple(movements), total_debit, total_credit,
        opening + total_debit - total_credit,
        tuple(dict.fromkeys(ids)),
    )
