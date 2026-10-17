"""ONE ERP Next: auditable *proposals* for weighted-average dispatch costing.

No database posting here. The valuation snapshot must be sourced from an
authenticated, locked, trusted inventory cost ledger in the SAME tenant and
currency. That ledger is not yet implemented: never treat this proposal as
a finalized accounting value or an external financial report.
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
class DispatchForCosting:
    event_id: UUID
    tenant_id: UUID
    product_id: UUID
    warehouse_id: UUID
    quantity: Decimal
    occurred_on: date
    currency: str
    kind: str = "sale_dispatch"


@dataclass(frozen=True, slots=True)
class CostSnapshot:
    tenant_id: UUID
    product_id: UUID
    warehouse_id: UUID
    currency: str
    on_hand: Decimal
    inventory_value: Decimal


@dataclass(frozen=True, slots=True)
class StockCostProposal:
    dispatch_event_id: UUID
    cogs: Decimal
    quantity_remaining: Decimal
    inventory_value_remaining: Decimal
    journal: JournalEntry


def propose_weighted_average_cogs(
    dispatch: DispatchForCosting, *,
    snapshot: CostSnapshot,
    chart: dict[str, Account],
    inventory_account_id: str,
    cogs_account_id: str,
    period: AccountingPeriod,
    minor_units: int = 2,
) -> StockCostProposal:
    """Return deterministic draft COGS entry against a trusted cost snapshot.

    Does not check warehouse history, create a cost layer, or persist anything.
    The actual service must lock and verify the snapshot and stock event, then
    record valuation and journal atomically with a unique event mapping.
    """
    if (not isinstance(dispatch.event_id, UUID)
            or not isinstance(dispatch.tenant_id, UUID)
            or not isinstance(dispatch.product_id, UUID)
            or not isinstance(dispatch.warehouse_id, UUID)
            or dispatch.kind != "sale_dispatch"
            or not isinstance(dispatch.occurred_on, date)
            or not isinstance(snapshot, CostSnapshot)
            or (dispatch.tenant_id,dispatch.product_id,dispatch.warehouse_id,dispatch.currency)
                != (snapshot.tenant_id,snapshot.product_id,snapshot.warehouse_id,snapshot.currency)):
        raise AccountingError("Valid same-company sales dispatch and valuation required")
    if minor_units not in (0, 2, 3):
        raise AccountingError("Invalid currency precision")
    money = Decimal(1).scaleb(-minor_units)
    qty_unit = Decimal("0.001")
    for value in (dispatch.quantity,snapshot.on_hand):
        if (not isinstance(value, Decimal) or not value.is_finite()
                or value < 0 or value != value.quantize(qty_unit)):
            raise AccountingError("Invalid stock costing quantity")
    if dispatch.quantity <= 0 or snapshot.on_hand <= 0 or dispatch.quantity > snapshot.on_hand:
        raise AccountingError("Dispatch exceeds positive valued stock on hand")
    value = snapshot.inventory_value
    if (not isinstance(value, Decimal) or not value.is_finite()
            or value <= 0 or value != value.quantize(money)):
        raise AccountingError("Costing requires a positive valued inventory snapshot")
    if inventory_account_id == cogs_account_id:
        raise AccountingError("Inventory and COGS accounts must differ")
    inventory = chart.get(inventory_account_id)
    cogs_account = chart.get(cogs_account_id)
    if (inventory is None or inventory.tenant_id != dispatch.tenant_id
            or inventory.kind is not AccountKind.ASSET or not inventory.active
            or not inventory.posting_allowed
            or cogs_account is None or cogs_account.tenant_id != dispatch.tenant_id
            or cogs_account.kind is not AccountKind.EXPENSE or not cogs_account.active
            or not cogs_account.posting_allowed):
        raise AccountingError("Invalid inventory or cost expense GL account")

    if dispatch.quantity == snapshot.on_hand:
        cost = value
    else:
        cost = (value * dispatch.quantity / snapshot.on_hand).quantize(
            money, rounding=ROUND_HALF_UP
        )
    if cost <= 0 or cost > value:
        raise AccountingError("Cost falls below supported monetary precision")
    left = value - cost
    entry = JournalEntry(
        id=dispatch.event_id, tenant_id=dispatch.tenant_id,
        reference=f"cogs:stock_dispatch:{dispatch.event_id}",
        occurred_on=dispatch.occurred_on, currency=dispatch.currency,
        idempotency_key=f"cogs:{dispatch.tenant_id}:{dispatch.event_id}",
        lines=(
            JournalLine(cogs_account_id, debit=cost),
            JournalLine(inventory_account_id, credit=cost),
        ),
    )
    validate_entry(entry, chart=chart, period=period, minor_units=minor_units)
    return StockCostProposal(
        dispatch.event_id, cost, snapshot.on_hand-dispatch.quantity, left, entry
    )
