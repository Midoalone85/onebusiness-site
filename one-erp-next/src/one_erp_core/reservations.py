"""Side-effect-free, tenant-scoped inventory reservation proposals.

Plans ONLY: no stock updates, payment capture, invoice posting, DB connection,
or approval are performed here. A transaction service must lock the balance and
the order line together, revalidate permissions and state, persist a unique
event ID, and update both counters atomically. Never trust caller-provided
stock positions for real fulfillment.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Mapping, Sequence
from uuid import UUID

from .accounting import AccountingError
from .commerce import (
    CommercialOrder, OrderKind, OrderStatus, Product, StockPosition, Warehouse,
    _quantity, validate_commercial_order,
)
from .tenancy import Action, Membership, can_access


class ReservationAction(str, Enum):
    HOLD = "hold"
    RELEASE = "release"


@dataclass(frozen=True, slots=True)
class ReservationPlan:
    event_id: UUID
    tenant_id: UUID
    order_id: UUID
    line_no: int
    action: ReservationAction
    quantity: Decimal
    stock_reserved_after: Decimal
    line_reserved_after: Decimal
    available_after: Decimal


def propose_reservation_change(
    order: CommercialOrder, *,
    action: ReservationAction,
    line_no: int,
    quantity: Decimal,
    position: StockPosition,
    previously_fulfilled: Decimal,
    line_reserved: Decimal,
    event_id: UUID,
    actor_id: UUID,
    memberships: tuple[Membership, ...],
    products: Mapping[UUID, Product],
    warehouses: Mapping[UUID, Warehouse],
    prior_event_ids: Sequence[UUID] = (),
    minor_units: int = 2,
) -> ReservationPlan:
    """Plan a hold/release; enforce caps and scope without mutating anything.

    line_reserved is the current total reservation owned by *this order line*;
    position.reserved includes holds from ALL lines sharing the stock bucket.
    Neither value is authoritative until re-read under database row locks.
    """
    validate_commercial_order(
        order, products=products, warehouses=warehouses, minor_units=minor_units
    )
    if (action not in (ReservationAction.HOLD, ReservationAction.RELEASE)
            or not isinstance(action, ReservationAction)
            or order.kind is not OrderKind.SALE
            or order.status is not OrderStatus.CONFIRMED):
        raise AccountingError("A confirmed sales order and valid action are required")
    if (not isinstance(actor_id, UUID) or not can_access(
            actor_id=actor_id,
            tenant_id=order.tenant_id,
            action=Action.PROCESS_STOCK,
            memberships=memberships)):
        raise AccountingError("Stock reservation authorization required")
    if (not isinstance(event_id, UUID) or event_id in prior_event_ids):
        raise AccountingError("Unique stock reservation event required")
    if not isinstance(line_no, int) or isinstance(line_no, bool):
        raise AccountingError("Invalid sales order line")
    line = next((x for x in order.lines if x.line_no == line_no), None)
    if line is None:
        raise AccountingError("Unknown sales order line")
    if (position.tenant_id != order.tenant_id
            or position.product_id != line.product_id
            or position.warehouse_id != line.warehouse_id):
        raise AccountingError("Cross-company or cross-warehouse reservation refused")

    qty = _quantity(quantity, positive=True)
    on_hand = _quantity(position.on_hand)
    stock_reserved = _quantity(position.reserved)
    fulfilled = _quantity(previously_fulfilled)
    owned_reserved = _quantity(line_reserved)
    if (stock_reserved > on_hand or owned_reserved > stock_reserved
            or fulfilled > line.quantity
            or owned_reserved > line.quantity - fulfilled):
        raise AccountingError("Inconsistent stock and reservation totals")

    if action is ReservationAction.HOLD:
        if qty > on_hand - stock_reserved:
            raise AccountingError("Not enough unreserved stock")
        if qty > line.quantity - fulfilled - owned_reserved:
            raise AccountingError("Reservation exceeds open order quantity")
        after_stock = stock_reserved + qty
        after_line = owned_reserved + qty
    else:
        if qty > owned_reserved:
            raise AccountingError("Cannot release stock not held by this line")
        after_stock = stock_reserved - qty
        after_line = owned_reserved - qty

    return ReservationPlan(
        event_id=event_id,
        tenant_id=order.tenant_id,
        order_id=order.id,
        line_no=line_no,
        action=action,
        quantity=qty,
        stock_reserved_after=after_stock,
        line_reserved_after=after_line,
        available_after=on_hand - after_stock,
    )
