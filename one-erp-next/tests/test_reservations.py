"""Unit contracts for draft-only reservation plans (no live stock mutation)."""
import unittest
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

from one_erp_core.accounting import AccountingError
from one_erp_core.commerce import (
    CommercialOrder, OrderKind, OrderLine, OrderStatus,
    Product, StockPosition, Warehouse,
)
from one_erp_core.reservations import (
    ReservationAction, propose_reservation_change,
)
from one_erp_core.tenancy import Membership, Role


class InventoryReservationPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tenant, self.other_tenant = uuid4(), uuid4()
        self.actor, self.accountant = uuid4(), uuid4()
        self.product, self.warehouse = uuid4(), uuid4()
        self.line = OrderLine(
            1, self.product, self.warehouse, Decimal("5.000"), Decimal("12.50")
        )
        self.order = CommercialOrder(
            uuid4(), self.tenant, "SO-001", "CUSTOMER-A",
            OrderKind.SALE, "SAR", (self.line,), OrderStatus.CONFIRMED,
        )
        self.position = StockPosition(
            self.tenant, self.product, self.warehouse,
            Decimal("10.000"), Decimal("3.000")
        )
        self.products = {
            self.product: Product(self.product, self.tenant, "SKU-1", "Part")
        }
        self.warehouses = {
            self.warehouse: Warehouse(self.warehouse, self.tenant, "MAIN")
        }
        self.memberships = (
            Membership(self.actor, self.tenant, Role.COMPANY_ADMIN),
            Membership(self.accountant, self.tenant, Role.ACCOUNTANT),
        )

    def plan(self, **kw):
        args = dict(
            action=ReservationAction.HOLD,
            line_no=1,
            quantity=Decimal("2.000"),
            position=self.position,
            previously_fulfilled=Decimal("0"),
            line_reserved=Decimal("1.000"),
            event_id=uuid4(),
            actor_id=self.actor,
            memberships=self.memberships,
            products=self.products,
            warehouses=self.warehouses,
        )
        args.update(kw)
        return propose_reservation_change(self.order, **args)

    def test_hold_uses_only_stock_not_held_for_another_order(self):
        plan = self.plan()
        self.assertEqual(plan.stock_reserved_after, Decimal("5.000"))
        self.assertEqual(plan.line_reserved_after, Decimal("3.000"))
        self.assertEqual(plan.available_after, Decimal("5.000"))
        self.assertEqual(plan.action, ReservationAction.HOLD)

    def test_partial_release_returns_units_to_available_stock(self):
        result = self.plan(action=ReservationAction.RELEASE,
                           quantity=Decimal("0.750"))
        self.assertEqual(result.stock_reserved_after, Decimal("2.250"))
        self.assertEqual(result.line_reserved_after, Decimal("0.250"))
        self.assertEqual(result.available_after, Decimal("7.750"))

    def test_hold_cannot_oversell_bucket_or_order_line(self):
        with self.assertRaises(AccountingError):
            self.plan(quantity=Decimal("8"))
        with self.assertRaises(AccountingError):
            self.plan(quantity=Decimal("5"))
        self.assertEqual(
            self.plan(quantity=Decimal("4")).line_reserved_after, Decimal("5")
        )

    def test_previously_delivered_units_reduce_new_reservation_cap(self):
        with self.assertRaises(AccountingError):
            self.plan(previously_fulfilled=Decimal("3"))
        self.assertEqual(
            self.plan(previously_fulfilled=Decimal("2"),
                      quantity=Decimal("2")).line_reserved_after, Decimal("3")
        )

    def test_cannot_release_a_different_orders_reservation(self):
        with self.assertRaises(AccountingError):
            self.plan(action=ReservationAction.RELEASE, quantity=Decimal("2"))
        with self.assertRaises(AccountingError):
            self.plan(line_reserved=Decimal("4"))

    def test_rejects_cross_tenant_and_wrong_location(self):
        for pos in (
            replace(self.position, tenant_id=self.other_tenant),
            replace(self.position, warehouse_id=uuid4()),
            replace(self.position, product_id=uuid4()),
        ):
            with self.subTest(position=pos), self.assertRaises(AccountingError):
                self.plan(position=pos)

    def test_only_active_company_stock_admin_can_reserve(self):
        with self.assertRaises(AccountingError):
            self.plan(actor_id=self.accountant)
        with self.assertRaises(AccountingError):
            self.plan(actor_id=self.actor, memberships=(
                Membership(self.actor, self.tenant, Role.COMPANY_ADMIN, is_active=False),
            ))
        with self.assertRaises(AccountingError):
            self.plan(actor_id=uuid4())

    def test_no_reservation_on_purchase_or_unconfirmed_sales(self):
        for order in (
            replace(self.order, kind=OrderKind.PURCHASE),
            replace(self.order, status=OrderStatus.DRAFT),
        ):
            with self.subTest(order=order), self.assertRaises(AccountingError):
                propose_reservation_change(
                    order, action=ReservationAction.HOLD, line_no=1,
                    quantity=Decimal("1"), position=self.position,
                    previously_fulfilled=Decimal("0"), line_reserved=Decimal("0"),
                    event_id=uuid4(), actor_id=self.actor,
                    memberships=self.memberships,
                    products=self.products, warehouses=self.warehouses,
                )

    def test_rejects_duplicate_event_and_invalid_actions(self):
        event_id = uuid4()
        with self.assertRaises(AccountingError):
            self.plan(event_id=event_id, prior_event_ids=(event_id,))
        with self.assertRaises(AccountingError):
            self.plan(action="hold")
        with self.assertRaises(AccountingError):
            self.plan(line_no=True)

    def test_rejects_invalid_quantities_or_inconsistent_state(self):
        cases = (
            {"quantity": Decimal("NaN")},
            {"quantity": Decimal("Infinity")},
            {"quantity": Decimal("0")},
            {"quantity": Decimal("-1")},
            {"quantity": Decimal("0.0001")},
            {"quantity": 1.5},
            {"line_reserved": Decimal("-1")},
            {"line_reserved": Decimal("9")},
            {"previously_fulfilled": Decimal("5.001")},
            {"position": replace(self.position, reserved=Decimal("11"))},
            {"position": replace(self.position, on_hand=Decimal("-1"))},
        )
        for patch in cases:
            with self.subTest(patch=patch), self.assertRaises(AccountingError):
                self.plan(**patch)

    def test_pure_domain_plan_does_not_change_source_objects(self):
        position = self.position
        order = self.order
        self.plan()
        self.assertEqual(position.reserved, Decimal("3.000"))
        self.assertEqual(order.lines[0].quantity, Decimal("5.000"))


if __name__ == "__main__":
    unittest.main()
