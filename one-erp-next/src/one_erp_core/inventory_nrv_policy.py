"""IAS 2 / IFRS as endorsed in KSA: source-evidenced NRV review proposals.

No IFRS compliance assertion: a trained reviewer must approve the
source estimate, item grouping, cost policy and financial period. This
module never posts journals or changes physical/valued inventory.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID

from .accounting import (
    Account, AccountKind, AccountingError, AccountingPeriod,
    JournalEntry, JournalLine, validate_entry,
)


@dataclass(frozen=True, slots=True)
class InventoryNRVPosition:
    tenant_id: UUID
    product_id: UUID
    warehouse_id: UUID
    currency: str
    quantity: Decimal
    cost_before_write_down: Decimal
    current_carrying_value: Decimal


@dataclass(frozen=True, slots=True)
class ApprovedNRVEstimate:
    tenant_id: UUID
    product_id: UUID
    warehouse_id: UUID
    currency: str
    per_unit_sale_price: Decimal
    per_unit_completion_cost: Decimal
    per_unit_selling_cost: Decimal
    evidence_reference: str
    assessed_on: date


@dataclass(frozen=True, slots=True)
class InventoryNRVProposal:
    original_cost: Decimal
    recoverable_nrv: Decimal
    carrying_value_after: Decimal
    write_down: Decimal
    reversal: Decimal
    journal: JournalEntry | None


def _safe_decimal(value: Decimal, *, unit: Decimal | None = None) -> Decimal:
    if (not isinstance(value, Decimal) or not value.is_finite()
        or value < 0 or (unit is not None and value != value.quantize(unit))):
        raise AccountingError("IAS 2 input must be a nonnegative finite Decimal")
    return value


def propose_inventory_nrv_adjustment(
    position: InventoryNRVPosition, *, estimate: ApprovedNRVEstimate,
    journal_id: UUID, inventory_account_id: str, write_down_account_id: str,
    chart: dict[str, Account], period: AccountingPeriod,
    minor_units: int = 2,
) -> InventoryNRVProposal:
    """Compute lower-of-cost-and-NRV write-down or capped reversal.

    Pre-existing write-down is original cost minus current carrying value.
    Raises rather than concealing cross-entity sources, negative NRV, stale
    carrying book amount or foreign-currency differences. No cost layers
    are created; no FIFO/specific-id attribution is inferred.
    """
    if minor_units not in (0, 2, 3):
        raise AccountingError("Unsupported report currency precision")
    if (not isinstance(position, InventoryNRVPosition)
        or not isinstance(estimate, ApprovedNRVEstimate)
        or not isinstance(journal_id, UUID)
        or not isinstance(estimate.assessed_on, date)
        or not isinstance(estimate.evidence_reference, str)
        or len(estimate.evidence_reference.strip()) < 3
        or (position.tenant_id, position.product_id, position.warehouse_id, position.currency)
         != (estimate.tenant_id, estimate.product_id, estimate.warehouse_id, estimate.currency)
        or not all(isinstance(x, UUID) for x in (
            position.tenant_id,position.product_id,position.warehouse_id))
        or not position.currency.isascii() or not position.currency.isalpha()
        or len(position.currency) != 3 or not position.currency.isupper()):
        raise AccountingError("Evidence-backed matching inventory assessment required")
    money = Decimal(1).scaleb(-minor_units)
    qty = _safe_decimal(position.quantity, unit=Decimal("0.001"))
    original = _safe_decimal(position.cost_before_write_down,unit=money)
    carried = _safe_decimal(position.current_carrying_value,unit=money)
    price = _safe_decimal(estimate.per_unit_sale_price)
    completion = _safe_decimal(estimate.per_unit_completion_cost)
    sell = _safe_decimal(estimate.per_unit_selling_cost)
    if qty <= 0 or original <= 0 or carried > original or carried <= 0:
        raise AccountingError("Valued positive inventory with valid prior write-down required")
    per_unit_nrv = price - completion - sell
    if per_unit_nrv < 0:
        raise AccountingError("Negative estimated NRV requires separate professional assessment")
    target = min(original, (per_unit_nrv * qty).quantize(money, rounding=ROUND_HALF_UP))
    if target < 0:
        raise AccountingError("Negative inventory measurement not supported")
    loss = max(Decimal("0"), carried-target)
    reversal = max(Decimal("0"), target-carried)
    if reversal > original-carried:
        raise AccountingError("Reversal cannot exceed historic write-down")
    if loss == 0 and reversal == 0:
        return InventoryNRVProposal(original,target,carried,loss,reversal,None)
    if inventory_account_id == write_down_account_id:
        raise AccountingError("Inventory and NRV expense accounts must differ")
    inventory = chart.get(inventory_account_id)
    expense = chart.get(write_down_account_id)
    if (inventory is None or inventory.tenant_id != position.tenant_id
        or inventory.kind is not AccountKind.ASSET
        or not inventory.active or not inventory.posting_allowed
        or expense is None or expense.tenant_id != position.tenant_id
        or expense.kind is not AccountKind.EXPENSE
        or not expense.active or not expense.posting_allowed):
        raise AccountingError("Valid scoped inventory asset and write-down expense required")
    amount = loss if loss > 0 else reversal
    lines = (
        (JournalLine(write_down_account_id,debit=amount),
         JournalLine(inventory_account_id,credit=amount)) if loss > 0
        else (JournalLine(inventory_account_id,debit=amount),
              JournalLine(write_down_account_id,credit=amount))
    )
    entry = JournalEntry(
        id=journal_id,tenant_id=position.tenant_id,
        reference=f"nrv:{estimate.evidence_reference.strip()}:{position.product_id}",
        occurred_on=estimate.assessed_on,currency=position.currency,
        idempotency_key=f"nrv:{position.tenant_id}:{journal_id}",
        lines=lines,
    )
    validate_entry(entry,chart=chart,period=period,minor_units=minor_units)
    return InventoryNRVProposal(original,target,target,loss,reversal,entry)
