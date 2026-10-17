"""Independent commercial workflows; no production data or network."""
import unittest
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

from one_erp_core.accounting import AccountingError
from one_erp_core.commerce import (
    CommercialOrder, FulfillmentProposal, OrderKind, OrderLine, OrderStatus,
    Product, StockPosition, Warehouse, propose_stock_fulfillment,
    validate_commercial_order,
)
from one_erp_core.tenancy import Action, Membership, Role, can_access


class CommercialFoundationTests(unittest.TestCase):
    def setUp(self):
        self.tenant, self.other = uuid4(), uuid4()
        self.admin, self.accountant = uuid4(), uuid4()
        self.product, self.warehouse = uuid4(), uuid4()
        self.products = {
            self.product: Product(self.product, self.tenant, "ITEM-001", "Stock item")
        }
        self.warehouses = {
            self.warehouse: Warehouse(self.warehouse, self.tenant, "MAIN")
        }
        self.line = OrderLine(1, self.product, self.warehouse,
                              Decimal("5.000"), Decimal("12.50"))
        self.sale = CommercialOrder(
            uuid4(), self.tenant, "SO-1", "CUSTOMER-1",
            OrderKind.SALE, "SAR", (self.line,), OrderStatus.CONFIRMED,
        )
        self.position = StockPosition(
            self.tenant, self.product, self.warehouse,
            on_hand=Decimal("8.000"), reserved=Decimal("2.000"),
        )
        self.memberships = (
            Membership(self.admin, self.tenant, Role.COMPANY_ADMIN),
            Membership(self.accountant, self.tenant, Role.ACCOUNTANT),
        )

    def check_order(self, order=None, *, products=None, warehouses=None, minor_units=2):
        return validate_commercial_order(
            order or self.sale,
            products=self.products if products is None else products,
            warehouses=self.warehouses if warehouses is None else warehouses,
            minor_units=minor_units,
        )

    def plan(self, order=None, **changes):
        fields = dict(
            line_no=1, quantity=Decimal("3.000"), position=self.position,
            previously_fulfilled=Decimal("0"),
            event_id=uuid4(), actor_id=self.admin, memberships=self.memberships,
            products=self.products, warehouses=self.warehouses,
        )
        fields.update(changes)
        return propose_stock_fulfillment(order or self.sale, **fields)

    def test_sales_and_purchases_share_product_warehouse_contract(self):
        sale = self.check_order()
        purchase = self.check_order(replace(self.sale, kind=OrderKind.PURCHASE))
        self.assertEqual(sale.net, Decimal("62.50"))
        self.assertEqual(sale, purchase)

    def test_decimal_line_net_uses_explicit_half_up_rounding(self):
        line = replace(self.line, quantity=Decimal("0.333"), unit_price=Decimal("0.05"))
        r = self.check_order(replace(self.sale, lines=(line,)))
        self.assertEqual(r.line_nets, (Decimal("0.02"),))

    def test_rejects_duplicate_line_numbers(self):
        with self.assertRaises(AccountingError):
            self.check_order(replace(self.sale, lines=(self.line, self.line)))

    def test_rejects_cross_tenant_product_and_warehouse(self):
        for products, warehouses in (
            ({self.product: replace(self.products[self.product], tenant_id=self.other)},
             self.warehouses),
            (self.products,
             {self.warehouse: replace(self.warehouses[self.warehouse], tenant_id=self.other)}),
        ):
            with self.assertRaises(AccountingError):
                self.check_order(products=products, warehouses=warehouses)

    def test_rejects_inactive_catalog(self):
        with self.assertRaises(AccountingError):
            self.check_order(products={
                self.product: replace(self.products[self.product], active=False)
            })
        with self.assertRaises(AccountingError):
            self.check_order(warehouses={
                self.warehouse: replace(self.warehouses[self.warehouse], active=False)
            })

    def test_rejects_invalid_parties_currency_and_status(self):
        for changes in (
            dict(partner_id=" "), dict(number=" "),
            dict(currency="sar"), dict(currency="SARS"),
            dict(kind="sale"), dict(status="confirmed"), dict(lines=()),
        ):
            with self.subTest(changes=changes), self.assertRaises(AccountingError):
                self.check_order(replace(self.sale, **changes))

    def test_rejects_negative_nan_float_and_over_precision(self):
        bads = (
            replace(self.line, quantity=Decimal("-1")),
            replace(self.line, quantity=Decimal("NaN")),
            replace(self.line, quantity=1.0),
            replace(self.line, quantity=Decimal("1.0001")),
            replace(self.line, unit_price=Decimal("-1")),
            replace(self.line, unit_price=Decimal("Infinity")),
            replace(self.line, unit_price=0.1),
            replace(self.line, unit_price=Decimal("1.001")),
        )
        for line in bads:
            with self.subTest(line=line), self.assertRaises(AccountingError):
                self.check_order(replace(self.sale, lines=(line,)))

    def test_currency_minor_unit_rules(self):
        self.assertEqual(
            self.check_order(replace(self.sale, currency="JPY", lines=(
                replace(self.line, unit_price=Decimal("11")),)), minor_units=0).net,
            Decimal("55"),
        )
        with self.assertRaises(AccountingError):
            self.check_order(minor_units=0)
        with self.assertRaises(AccountingError):
            self.check_order(minor_units=5)

    def test_sales_dispatch_reduces_only_unreserved_stock(self):
        plan = self.plan()
        self.assertEqual(plan.movement_kind, "sale_dispatch")
        self.assertEqual(plan.stock_after, Decimal("5.000"))
        self.assertEqual(plan.fulfilled_after, Decimal("3.000"))

    def test_sales_cannot_dispatch_reserved_units(self):
        with self.assertRaises(AccountingError):
            self.plan(quantity=Decimal("7"))
        self.assertEqual(self.plan(quantity=Decimal("5")).stock_after, Decimal("3"))

    def test_purchase_receipt_increases_stock(self):
        purchase = replace(self.sale, kind=OrderKind.PURCHASE)
        plan = self.plan(purchase, quantity=Decimal("5"))
        self.assertEqual(plan.movement_kind, "purchase_receipt")
        self.assertEqual(plan.stock_after, Decimal("13.000"))

    def test_partial_delivery_respects_outstanding_quantity(self):
        self.assertEqual(
            self.plan(quantity=Decimal("2"), previously_fulfilled=Decimal("3"))
            .fulfilled_after, Decimal("5"),
        )
        with self.assertRaises(AccountingError):
            self.plan(quantity=Decimal("3"), previously_fulfilled=Decimal("3"))

    def test_draft_order_may_not_move_stock(self):
        with self.assertRaises(AccountingError):
            self.plan(replace(self.sale, status=OrderStatus.DRAFT))

    def test_accountant_cannot_dispatch_warehouse_stock(self):
        self.assertFalse(can_access(
            actor_id=self.accountant, tenant_id=self.tenant,
            action=Action.PROCESS_STOCK, memberships=self.memberships,
        ))
        with self.assertRaises(AccountingError):
            self.plan(actor_id=self.accountant)

    def test_other_tenant_actor_and_disabled_admin_denied(self):
        with self.assertRaises(AccountingError):
            self.plan(actor_id=uuid4())
        with self.assertRaises(AccountingError):
            self.plan(memberships=(Membership(
                self.admin, self.tenant, Role.COMPANY_ADMIN, is_active=False),))

    def test_stock_position_must_match_exact_location(self):
        for changes in (
            dict(tenant_id=self.other), dict(product_id=uuid4()),
            dict(warehouse_id=uuid4()),
        ):
            with self.subTest(changes=changes), self.assertRaises(AccountingError):
                self.plan(position=replace(self.position, **changes))

    def test_corrupt_or_invalid_stock_balances_rejected(self):
        for changes in (
            dict(on_hand=Decimal("-1")), dict(on_hand=Decimal("NaN")),
            dict(reserved=Decimal("9")), dict(reserved=Decimal("-1")),
            dict(on_hand=Decimal("8.0001")),
        ):
            with self.subTest(changes=changes), self.assertRaises(AccountingError):
                self.plan(position=replace(self.position, **changes))

    def test_stock_movement_quantity_must_be_positive_decimal(self):
        for qty in (Decimal("0"), Decimal("-1"), Decimal("NaN"), 1.0,
                    Decimal("0.0001")):
            with self.subTest(qty=qty), self.assertRaises(AccountingError):
                self.plan(quantity=qty)

    def test_duplicate_stock_event_rejected(self):
        event_id = uuid4()
        with self.assertRaises(AccountingError):
            self.plan(event_id=event_id, prior_event_ids=(event_id,))

    def test_receipt_rejects_movement_past_remaining_order(self):
        with self.assertRaises(AccountingError):
            self.plan(replace(self.sale, kind=OrderKind.PURCHASE),
                      quantity=Decimal("4"), previously_fulfilled=Decimal("2"))

    def test_purchase_then_sale_proposals_preserve_stock_arithmetic(self):
        purchase = replace(self.sale, kind=OrderKind.PURCHASE)
        incoming = self.plan(purchase, quantity=Decimal("5"))
        updated = replace(self.position, on_hand=incoming.stock_after)
        outgoing = self.plan(position=updated, quantity=Decimal("4"))
        self.assertEqual(outgoing.stock_after, Decimal("9.000"))
        # Domain outputs only. A persistent order/stock service must still
        # verify current state under database locks before committing.


if __name__ == "__main__":
    unittest.main()
