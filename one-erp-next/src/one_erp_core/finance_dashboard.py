"""Read-only finance dashboard bundle, built from one verified ledger snapshot.

No API endpoint, user authentication, browser session or database access is
provided by this module; it composes already validated report domain rules.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Mapping
from uuid import UUID

from .accounting import Account, AccountingError, JournalEntry
from .finance_insights import ComparativeProfitAndLoss, compare_profit_and_loss
from .financial_reports import TrialBalance, trial_balance
from .statement_details import BalanceSheet, balance_sheet
from .report_repository import LedgerSnapshot


@dataclass(frozen=True, slots=True)
class FinanceDashboard:
    tenant_id: UUID
    as_of: date
    from_date: date
    currency: str
    balance_sheet: BalanceSheet
    trial_balance: TrialBalance
    comparative_income: ComparativeProfitAndLoss
    journal_count: int
    source_ids: tuple[UUID, ...]


def build_finance_dashboard(
    snapshot: LedgerSnapshot,
    *,
    from_date: date,
    currency: str,
) -> FinanceDashboard:
    """Require an exact as-of snapshot; refuse to guess missing historical data."""
    if not isinstance(snapshot, LedgerSnapshot):
        raise AccountingError("Verified finance snapshot required")
    if from_date > snapshot.as_of:
        raise AccountingError("Reporting start cannot follow as-of date")
    sheet = balance_sheet(
        tenant_id=snapshot.tenant_id,
        as_of=snapshot.as_of,
        currency=currency,
        chart=snapshot.chart,
        entries=snapshot.entries,
        actor_id=snapshot.actor_id,
        memberships=snapshot.memberships,
    )
    trial = trial_balance(
        tenant_id=snapshot.tenant_id,
        as_of=snapshot.as_of,
        currency=currency,
        chart=snapshot.chart,
        entries=snapshot.entries,
    )
    comparison = compare_profit_and_loss(
        tenant_id=snapshot.tenant_id,
        current_from=from_date,
        current_through=snapshot.as_of,
        currency=currency,
        chart=snapshot.chart,
        entries=snapshot.entries,
        actor_id=snapshot.actor_id,
        memberships=snapshot.memberships,
    )
    if not sheet.balanced or not trial.balanced:
        raise AccountingError("Cannot publish inconsistent ledger report")
    # The collection was approved and validated by balance_sheet; the bundle
    # reuses those source IDs and same in-memory transaction snapshot.
    return FinanceDashboard(
        snapshot.tenant_id,snapshot.as_of,from_date,currency,
        sheet,trial,comparison,len(sheet.source_entry_ids),
        sheet.source_entry_ids,
    )
