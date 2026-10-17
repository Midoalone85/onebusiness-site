"""Explainable, read-only financial comparison between two equal-duration periods.

Not investment guidance or AI-generated facts. Values derive solely from
validated, tenant-scoped posted journal records and existing P&L policies.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Mapping, Sequence
from uuid import UUID

from .accounting import Account, AccountingError, JournalEntry
from .financial_reports import ProfitAndLoss, profit_and_loss
from .statement_details import _posted_source
from .tenancy import Membership


@dataclass(frozen=True, slots=True)
class ComparisonMetric:
    name: str
    current: Decimal
    previous: Decimal
    absolute_change: Decimal
    percent_change: Decimal | None


@dataclass(frozen=True, slots=True)
class FinancialInsight:
    code: str
    severity: str
    explanation: str
    evidence: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class ComparativeProfitAndLoss:
    current: ProfitAndLoss
    previous: ProfitAndLoss
    revenue: ComparisonMetric
    expense: ComparisonMetric
    net_profit: ComparisonMetric
    flags: tuple[FinancialInsight, ...]


def _metric(name: str, current: Decimal, previous: Decimal) -> ComparisonMetric:
    difference = current - previous
    return ComparisonMetric(
        name, current, previous, difference,
        None if previous == 0 else (difference / abs(previous) * 100),
    )


def compare_profit_and_loss(
    *, tenant_id: UUID, current_from: date, current_through: date,
    currency: str, chart: Mapping[str, Account],
    entries: Sequence[JournalEntry], actor_id: UUID,
    memberships: tuple[Membership, ...],
    expense_growth_alert_pct: Decimal = Decimal("25"),
    revenue_drop_alert_pct: Decimal = Decimal("15"),
) -> ComparativeProfitAndLoss:
    """Compare adjacent non-overlapping windows, preserving exact source IDs.

    Alert thresholds are explicitly provided by the caller. Flags are not
    factual accusations of fraud and can be caused by ordinary seasonality.
    """
    if current_from > current_through:
        raise AccountingError("Current comparison period is invalid")
    for n in (expense_growth_alert_pct, revenue_drop_alert_pct):
        if not isinstance(n, Decimal) or not n.is_finite() or n < 0:
            raise AccountingError("Thresholds must be finite nonnegative Decimal")
    days = (current_through - current_from).days + 1
    previous_through = current_from - timedelta(days=1)
    previous_from = current_from - timedelta(days=days)

    # Authorize and validate source BEFORE filtering out historical records.
    _posted_source(
        tenant_id=tenant_id, currency=currency, as_of=current_through,
        chart=chart, entries=entries, actor_id=actor_id,
        memberships=memberships,
    )
    previous = profit_and_loss(
        tenant_id=tenant_id, from_date=previous_from,
        through_date=previous_through, currency=currency,
        chart=chart, entries=entries,
    )
    current = profit_and_loss(
        tenant_id=tenant_id, from_date=current_from,
        through_date=current_through, currency=currency,
        chart=chart, entries=entries,
    )
    revenue = _metric("revenue",current.revenue,previous.revenue)
    expense = _metric("expense",current.expense,previous.expense)
    net_profit = _metric("net_profit",current.net_profit,previous.net_profit)
    flags = []
    if expense.percent_change is not None and expense.percent_change >= expense_growth_alert_pct:
        flags.append(FinancialInsight(
            "EXPENSE_RISE", "review",
            "Expenses grew above configured threshold. Inspect source journals and seasonality.",
            current.source_entry_ids,
        ))
    if revenue.percent_change is not None and revenue.percent_change <= -revenue_drop_alert_pct:
        flags.append(FinancialInsight(
            "REVENUE_DECLINE", "review",
            "Revenue declined beyond configured threshold. Inspect sales mix and timing.",
            current.source_entry_ids,
        ))
    if current.net_profit < 0:
        flags.append(FinancialInsight(
            "NET_LOSS", "review",
            "Current period expenses exceed revenue.",
            current.source_entry_ids,
        ))
    return ComparativeProfitAndLoss(
        current,previous,revenue,expense,net_profit,tuple(flags),
    )
