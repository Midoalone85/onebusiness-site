"""IAS 2 NRV proposal regression checks; no ledger writes or tax treatment."""
import unittest
from dataclasses import replace
from datetime import date
from decimal import Decimal as D
from uuid import uuid4

from one_erp_core.accounting import Account, AccountKind, AccountingError, AccountingPeriod
from one_erp_core.inventory_nrv_policy import (
    InventoryNRVPosition,ApprovedNRVEstimate,propose_inventory_nrv_adjustment
)


class IAS2InventoryNRVTests(unittest.TestCase):
    def setUp(self):
        self.tenant,self.item,self.warehouse=uuid4(),uuid4(),uuid4()
        self.position=InventoryNRVPosition(
            self.tenant,self.item,self.warehouse,"SAR",
            D("4"),D("100.00"),D("100.00"))
        self.estimate=ApprovedNRVEstimate(
            self.tenant,self.item,self.warehouse,"SAR",
            D("20"),D("0"),D("2"),"APPROVED-NRV-2026",date(2026,10,15))
        self.period=AccountingPeriod(
            self.tenant,date(2026,1,1),date(2027,1,1))
        self.chart={
            "inv":Account("inv",self.tenant,"140","Inventory",AccountKind.ASSET),
            "write_down":Account("write_down",self.tenant,"520","NRV loss",AccountKind.EXPENSE)
        }

    def check(self,position=None,estimate=None,period=None,chart=None):
        return propose_inventory_nrv_adjustment(
            position or self.position,estimate=estimate or self.estimate,
            journal_id=uuid4(),inventory_account_id="inv",
            write_down_account_id="write_down",
            chart=self.chart if chart is None else chart,
            period=self.period if period is None else period,
        )

    def test_cost_100_and_nrv_72_proposes_write_down_of_28(self):
        result=self.check()
        self.assertEqual(result.recoverable_nrv,D("72"))
        self.assertEqual(result.write_down,D("28"))
        self.assertEqual(result.reversal,D("0"))
        self.assertEqual(result.journal.lines[0].debit,D("28"))
        self.assertEqual(result.journal.lines[1].credit,D("28"))

    def test_later_recovery_to_96_reverses_only_24(self):
        position=replace(self.position,current_carrying_value=D("72"))
        result=self.check(position=position,estimate=replace(
            self.estimate,per_unit_sale_price=D("26")))
        self.assertEqual(result.carrying_value_after,D("96"))
        self.assertEqual(result.reversal,D("24"))
        self.assertEqual(result.journal.lines[0].debit,D("24"))
        self.assertEqual(result.journal.lines[1].credit,D("24"))

    def test_full_write_down_to_zero_may_recover_but_not_above_cost(self):
        depleted=replace(self.position,current_carrying_value=D("0"))
        result=self.check(position=depleted)
        self.assertEqual(result.reversal,D("72"))
        self.assertEqual(result.carrying_value_after,D("72"))
        self.assertEqual(result.journal.lines[0].debit,D("72"))
        zero=self.check(position=depleted,estimate=replace(
            self.estimate,per_unit_sale_price=D("0"),
            per_unit_selling_cost=D("0")))
        self.assertEqual(zero.carrying_value_after,D("0"))
        self.assertIsNone(zero.journal)

    def test_reversal_never_exceeds_original_cost(self):
        position=replace(self.position,current_carrying_value=D("72"))
        result=self.check(position=position,estimate=replace(
            self.estimate,per_unit_sale_price=D("100")))
        self.assertEqual(result.carrying_value_after,D("100"))
        self.assertEqual(result.reversal,D("28"))

    def test_no_write_down_if_nrv_above_cost(self):
        result=self.check(estimate=replace(
            self.estimate,per_unit_sale_price=D("80")))
        self.assertIsNone(result.journal)
        self.assertEqual(result.carrying_value_after,D("100"))

    def test_negative_nrv_requires_a_separate_review(self):
        with self.assertRaises(AccountingError):
            self.check(estimate=replace(self.estimate,
                per_unit_sale_price=D("1"),per_unit_selling_cost=D("5")))

    def test_cross_tenant_and_currency_mismatch_are_refused(self):
        for revised in (replace(self.estimate,tenant_id=uuid4()),
                        replace(self.estimate,currency="USD"),
                        replace(self.estimate,product_id=uuid4()),
                        replace(self.estimate,warehouse_id=uuid4())):
            with self.assertRaises(AccountingError):
                self.check(estimate=revised)

    def test_invalid_quantity_and_inventory_cost_precision_refused(self):
        for qty in (D("0"),D("-1"),D("NaN"),D("2.0001")):
            with self.assertRaises(AccountingError):
                self.check(position=replace(self.position,quantity=qty))
        with self.assertRaises(AccountingError):
            self.check(position=replace(self.position,
                current_carrying_value=D("105")))
        with self.assertRaises(AccountingError):
            self.check(position=replace(self.position,
                cost_before_write_down=D("100.001")))

    def test_closed_or_wrong_period_refused_even_when_no_adjustment(self):
        high=replace(self.estimate,per_unit_sale_price=D("100"))
        for period in (
            replace(self.period,closed=True),
            replace(self.period,tenant_id=uuid4()),
            AccountingPeriod(self.tenant,date(2025,1,1),date(2026,1,1)),
        ):
            with self.assertRaises(AccountingError):
                self.check(estimate=high,period=period)

    def test_invalid_accounts_and_missing_approval_evidence_refused(self):
        with self.assertRaises(AccountingError):
            self.check(chart={
                "inv":self.chart["write_down"],
                "write_down":self.chart["inv"]})
        with self.assertRaises(AccountingError):
            self.check(estimate=replace(self.estimate,evidence_reference=""))

    def test_unsupported_float_and_negative_cost_to_sell_are_refused(self):
        with self.assertRaises(AccountingError):
            self.check(estimate=replace(self.estimate,per_unit_sale_price=20.0))
        with self.assertRaises(AccountingError):
            self.check(estimate=replace(self.estimate,per_unit_selling_cost=D("-1")))


if __name__ == "__main__":
    unittest.main()
