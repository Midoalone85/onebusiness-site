"""Original ONE ERP Next commercial order and stock movement policies.

This is a deliberately pure domain layer: no SQL access, external shipment,
order confirmation, invoice issuance, or inventory mutation. Real execution
MUST lock order lines and stock balances transactionally and enforce
(tenant, event_id) uniqueness in PostgreSQL. No regulatory tax calculation
or accounting posting is implied.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Mapping, Sequence
from uuid import UUID

from .accounting import AccountingError
from .tenancy import Action, Membership, can_access


class OrderKind(str, Enum):
    SALE = "sale"
    PURCHASE = "purchase"


class OrderStatus(str, Enum):
    DRAFT = "draft"
    CONFIRMED = "confirmed"


@dataclass(frozen=True, slots=True)
class Product:
    id: UUID
    tenant_id: UUID
    sku: str
    name: str
    active: bool = True


@dataclass(frozen=True, slots=True)
class Warehouse:
    id: UUID
    tenant_id: UUID
    code: str
    active: bool = True


@dataclass(frozen=True, slots=True)
class OrderLine:
    line_no: int
    product_id: UUID
    warehouse_id: UUID
    quantity: Decimal
    unit_price: Decimal


@dataclass(frozen=True, slots=True)
class CommercialOrder:
    id: UUID
    tenant_id: UUID
    number: str
    partner_id: str
    kind: OrderKind
    currency: str
    lines: tuple[OrderLine, ...]
    status: OrderStatus = OrderStatus.DRAFT


@dataclass(frozen=True, slots=True)
class StockPosition:
    tenant_id: UUID
    product_id: UUID
    warehouse_id: UUID
    on_hand: Decimal
    reserved: Decimal = Decimal("0")


@dataclass(frozen=True, slots=True)
class OrderTotals:
    net: Decimal
    line_nets: tuple[Decimal, ...]


@dataclass(frozen=True, slots=True)
class FulfillmentProposal:
    event_id: UUID
    tenant_id: UUID
    order_id: UUID
    line_no: int
    movement_kind: str
    quantity: Decimal
    stock_after: Decimal
    fulfilled_after: Decimal


def _quantity(value: Decimal, *, positive: bool = False) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise AccountingError("Quantities must be finite Decimal values")
    if value < 0 or (positive and value == 0) or value != value.quantize(Decimal("0.001")):
        raise AccountingError("Invalid or unsupported quantity precision")
    return value


def _price(value: Decimal, minor_units: int) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise AccountingError("Prices must be nonnegative finite Decimal values")
    if value != value.quantize(Decimal(1).scaleb(-minor_units)):
        raise AccountingError("Price precision incompatible with currency")
    return value


def validate_commercial_order(
    order: CommercialOrder, *,
    products: Mapping[UUID, Product],
    warehouses: Mapping[UUID, Warehouse],
    minor_units: int = 2,
) -> OrderTotals:
    """Validate a stockable-goods-only order and calculate illustrative net."""
    if minor_units not in (0, 2, 3):
        raise AccountingError("Unsupported currency precision")
    if not isinstance(order.kind, OrderKind) or not isinstance(order.status, OrderStatus):
        raise AccountingError("Unsupported commercial order kind/status")
    if (not isinstance(order.id, UUID) or not isinstance(order.tenant_id, UUID)
            or not isinstance(order.number, str) or not order.number.strip()
            or not isinstance(order.partner_id, str) or not order.partner_id.strip()):
        raise AccountingError("Complete commercial order identity required")
    if (not isinstance(order.currency, str) or len(order.currency) != 3
            or not order.currency.isascii() or not order.currency.isalpha()
            or order.currency != order.currency.upper()):
        raise AccountingError("Invalid currency code")
    if not order.lines or len(order.lines) > 1000:
        raise AccountingError("Order must have 1..1000 lines")
    if len({row.line_no for row in order.lines}) != len(order.lines):
        raise AccountingError("Duplicate order line numbers")
    unit = Decimal(1).scaleb(-minor_units)
    amounts = []
    for line in order.lines:
        if not isinstance(line.line_no, int) or isinstance(line.line_no, bool) or line.line_no <= 0:
            raise AccountingError("Positive order line number required")
        product = products.get(line.product_id)
        warehouse = warehouses.get(line.warehouse_id)
        if (product is None or warehouse is None
                or product.tenant_id != order.tenant_id
                or warehouse.tenant_id != order.tenant_id
                or not product.active or not warehouse.active
                or not product.sku.strip() or not product.name.strip()
                or not warehouse.code.strip()):
            raise AccountingError("Unknown, inactive, or cross-company product/warehouse")
        qty = _quantity(line.quantity, positive=True)
        price = _price(line.unit_price, minor_units)
        amounts.append((qty * price).quantize(unit, rounding=ROUND_HALF_UP))
    return OrderTotals(net=sum(amounts, Decimal("0")), line_nets=tuple(amounts))


def propose_stock_fulfillment(
    order: CommercialOrder, *, line_no: int, quantity: Decimal,
    position: StockPosition, previously_fulfilled: Decimal,
    event_id: UUID, actor_id: UUID, memberships: tuple[Membership, ...],
    products: Mapping[UUID, Product], warehouses: Mapping[UUID, Warehouse],
    prior_event_ids: Sequence[UUID] = (), minor_units: int = 2,
) -> FulfillmentProposal:
    """Propose purchase receipt or sale dispatch without mutating stock.

    Sales dispatch cannot exceed unreserved available stock. Purchase receipts
    cannot exceed outstanding order quantities. Caller must recheck *within
    a PostgreSQL transaction with row locks* before changing any stock.
    """
    validate_commercial_order(
        order, products=products, warehouses=warehouses, minor_units=minor_units
    )
    if order.status != OrderStatus.CONFIRMED:
        raise AccountingError("Only confirmed orders may move stock")
    if (not isinstance(actor_id, UUID) or not can_access(
            actor_id=actor_id, tenant_id=order.tenant_id,
            action=Action.PROCESS_STOCK, memberships=memberships)):
        raise AccountingError("Company stock-operation authorization required")
    if not isinstance(event_id, UUID) or event_id in prior_event_ids:
        raise AccountingError("Stock movement event must be new and valid")
    line = next((v for v in order.lines if v.line_no == line_no), None)
    if line is None:
        raise AccountingError("Unknown order line")
    if (position.tenant_id != order.tenant_id
            or position.product_id != line.product_id
            or position.warehouse_id != line.warehouse_id):
        raise AccountingError("Stock position belongs to a different company/location")
    qty = _quantity(quantity, positive=True)
    fulfilled = _quantity(previously_fulfilled)
    on_hand = _quantity(position.on_hand)
    reserved = _quantity(position.reserved)
    if reserved > on_hand or fulfilled > line.quantity:
        raise AccountingError("Corrupt stock position or order fulfillment")
    if qty > line.quantity - fulfilled:
        raise AccountingError("Movement exceeds remaining ordered quantity")
    if order.kind == OrderKind.SALE:
        if qty > on_hand - reserved:
            raise AccountingError("Insufficient available stock for dispatch")
        stock_after = on_hand - qty
        movement = "sale_dispatch"
    else:
        stock_after = on_hand + qty
        movement = "purchase_receipt"
    if stock_after > Decimal("99999999999999999.999"):
        raise AccountingError("Stock balance exceeds database numeric capacity")
    return FulfillmentProposal(
        event_id=event_id, tenant_id=order.tenant_id, order_id=order.id,
        line_no=line_no, movement_kind=movement, quantity=qty,
        stock_after=stock_after, fulfilled_after=fulfilled + qty,
    )
