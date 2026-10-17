"""Policy-only inventory valuation checks: no DB mutation and no live posting."""
import unittest
from dataclasses import replace
from datetime import date
from decimal import Decimal
from uuid import uuid4

from one_erp_core.accounting import (
    Account,AccountKind,AccountingError,AccountingPeriod,EntryStatus,
)
from one_erp_core.stock_costing import (
    DispatchForCosting,CostSnapshot,propose_weighted_average_cogs,
)


class WeightedAverageCostingTests(unittest.TestCase):
    def setUp(self):
        self.tenant=uuid4()
        self.product=uuid4()
        self.warehouse=uuid4()
        self.dispatch=DispatchForCosting(
            uuid4(),self.tenant,self.product,self.warehouse,
            Decimal("3.000"),date(2026,10,15),"SAR",
        )
        self.snapshot=CostSnapshot(
            self.tenant,self.product,self.warehouse,
            "SAR",Decimal("10.000"),Decimal("250.00"),
        )
        self.period=AccountingPeriod(
            self.tenant,date(2026,1,1),date(2027,1,1),
        )
        self.chart={
            "inventory":Account("inventory",self.tenant,"140",
                                "Stock",AccountKind.ASSET),
            "cogs":Account("cogs",self.tenant,"510",
                           "Cost of sales",AccountKind.EXPENSE),
        }

    def plan(self,dispatch=None,snapshot=None,chart=None,minor_units=2,
             inventory_account_id="inventory",cogs_account_id="cogs",
             period=None):
        return propose_weighted_average_cogs(
            dispatch or self.dispatch,snapshot=snapshot or self.snapshot,
            chart=self.chart if chart is None else chart,
            inventory_account_id=inventory_account_id,
            cogs_account_id=cogs_account_id,
            period=period or self.period,minor_units=minor_units,
        )

    def test_sales_dispatch_yields_balanced_cogs_and_remaining_value(self):
        p=self.plan()
        self.assertEqual(p.cogs,Decimal("75.00"))
        self.assertEqual(p.quantity_remaining,Decimal("7.000"))
        self.assertEqual(p.inventory_value_remaining,Decimal("175.00"))
        self.assertEqual(p.journal.status,EntryStatus.DRAFT)
        self.assertEqual(
            [(v.account_id,v.debit,v.credit) for v in p.journal.lines],
            [("cogs",Decimal("75.00"),Decimal("0")),
             ("inventory",Decimal("0"),Decimal("75.00"))],
        )
        self.assertEqual(p.journal.idempotency_key,
                         f"cogs:{self.tenant}:{self.dispatch.event_id}")

    def test_full_dispatch_consumes_full_valuation_without_rounding_remnants(self):
        p=self.plan(dispatch=replace(self.dispatch,quantity=Decimal("10")))
        self.assertEqual(p.cogs,Decimal("250.00"))
        self.assertEqual(p.inventory_value_remaining,Decimal("0"))

    def test_rounds_cost_half_up_in_currency_units(self):
        snap=replace(self.snapshot,on_hand=Decimal("3"),
                     inventory_value=Decimal("10.00"))
        event=replace(self.dispatch,quantity=Decimal("1"))
        p=self.plan(dispatch=event,snapshot=snap)
        self.assertEqual(p.cogs,Decimal("3.33"))
        self.assertEqual(p.inventory_value_remaining,Decimal("6.67"))

    def test_cross_tenant_product_warehouse_and_currency_denied(self):
        for patch in (
            dict(tenant_id=uuid4()),dict(product_id=uuid4()),
            dict(warehouse_id=uuid4()),dict(currency="USD"),
        ):
            with self.subTest(patch=patch),self.assertRaises(AccountingError):
                self.plan(snapshot=replace(self.snapshot,**patch))

    def test_only_sale_dispatch_movement_is_valued(self):
        for kind in ("purchase_receipt","stock_transfer",""):
            with self.subTest(kind=kind),self.assertRaises(AccountingError):
                self.plan(dispatch=replace(self.dispatch,kind=kind))

    def test_reject_negative_non_finite_and_fractional_quantities(self):
        for value in (Decimal("-1"),Decimal("NaN"),Decimal("Infinity"),
                      Decimal("0.0001"),1.0):
            with self.subTest(value=value),self.assertRaises(AccountingError):
                self.plan(dispatch=replace(self.dispatch,quantity=value))
        with self.assertRaises(AccountingError):
            self.plan(dispatch=replace(self.dispatch,quantity=Decimal("11")))
        with self.assertRaises(AccountingError):
            self.plan(dispatch=replace(self.dispatch,quantity=Decimal("0")))

    def test_reject_invalid_inventory_snapshot(self):
        for v in (Decimal("-1"),Decimal("0"),Decimal("NaN"),
                  Decimal("Infinity"),Decimal("25.001"),25.0):
            with self.subTest(value=v),self.assertRaises(AccountingError):
                self.plan(snapshot=replace(self.snapshot,inventory_value=v))
        with self.assertRaises(AccountingError):
            self.plan(snapshot=replace(self.snapshot,on_hand=Decimal("0")))

    def test_reject_wrong_gl_types_or_account_company(self):
        for account_patch in (
            dict(kind=AccountKind.REVENUE),
            dict(tenant_id=uuid4()),dict(active=False),
            dict(posting_allowed=False),
        ):
            chart=dict(self.chart)
            chart["cogs"]=replace(chart["cogs"],**account_patch)
            with self.subTest(patch=account_patch),self.assertRaises(AccountingError):
                self.plan(chart=chart)
        with self.assertRaises(AccountingError):
            self.plan(inventory_account_id="cogs",cogs_account_id="cogs")

    def test_period_guards_still_apply(self):
        with self.assertRaises(AccountingError):
            self.plan(period=replace(self.period,closed=True))
        with self.assertRaises(AccountingError):
            self.plan(dispatch=replace(self.dispatch,occurred_on=date(2027,1,1)))

    def test_rounding_to_zero_cost_is_not_silently_posted(self):
        snap=replace(self.snapshot,on_hand=Decimal("10000"),
                     inventory_value=Decimal("1.00"))
        event=replace(self.dispatch,quantity=Decimal("0.001"))
        with self.assertRaises(AccountingError):
            self.plan(dispatch=event,snapshot=snap)

    def test_policy_never_mutates_dispatch_snapshot_or_chart(self):
        d=self.dispatch
        s=self.snapshot
        original=self.chart.copy()
        self.plan()
        self.assertEqual(d,self.dispatch)
        self.assertEqual(s,self.snapshot)
        self.assertEqual(self.chart,original)


if __name__=="__main__":
    unittest.main()
