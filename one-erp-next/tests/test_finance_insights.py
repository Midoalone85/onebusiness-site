import unittest
from dataclasses import replace
from datetime import date
from decimal import Decimal
from uuid import uuid4

from one_erp_core.accounting import (
    Account,AccountKind,AccountingError,EntryStatus,JournalEntry,JournalLine
)
from one_erp_core.finance_insights import compare_profit_and_loss
from one_erp_core.tenancy import Membership, Role


class FinanceInsightTests(unittest.TestCase):
    def setUp(self):
        self.tenant,self.actor=uuid4(),uuid4()
        self.chart={
            "cash":Account("cash",self.tenant,"100","Cash",AccountKind.ASSET),
            "sales":Account("sales",self.tenant,"400","Revenue",AccountKind.REVENUE),
            "expense":Account("expense",self.tenant,"500","Expense",AccountKind.EXPENSE),
        }
        def event(dt,ref,account,amount):
            if account=="sales":
                lines=(JournalLine("cash",debit=Decimal(amount)),
                       JournalLine("sales",credit=Decimal(amount)))
            else:
                lines=(JournalLine("expense",debit=Decimal(amount)),
                       JournalLine("cash",credit=Decimal(amount)))
            return JournalEntry(uuid4(),self.tenant,ref,dt,"SAR",ref,
                                lines,EntryStatus.POSTED)
        self.entries=[
            event(date(2026,8,15),"S1","sales","200"),
            event(date(2026,8,16),"E1","expense","60"),
            event(date(2026,9,15),"S2","sales","100"),
            event(date(2026,9,16),"E2","expense","90"),
        ]
        self.memberships=(Membership(self.actor,self.tenant,Role.VIEWER),)

    def compare(self,**kw):
        args=dict(tenant_id=self.tenant,
                  current_from=date(2026,9,1),
                  current_through=date(2026,9,30),
                  currency="SAR",chart=self.chart,entries=self.entries,
                  actor_id=self.actor,memberships=self.memberships)
        args.update(kw)
        return compare_profit_and_loss(**args)

    def test_equal_duration_adjacent_periods_and_evidence(self):
        result=self.compare()
        self.assertEqual(result.previous.from_date,date(2026,8,2))
        self.assertEqual(result.previous.through_date,date(2026,8,31))
        self.assertEqual(result.current.revenue,Decimal("100"))
        self.assertEqual(result.previous.revenue,Decimal("200"))
        self.assertEqual(result.revenue.percent_change,Decimal("-50"))
        self.assertEqual(result.expense.percent_change,Decimal("50"))
        self.assertEqual([x.code for x in result.flags],["EXPENSE_RISE","REVENUE_DECLINE"])
        self.assertEqual(set(result.flags[0].evidence),
                         set(result.current.source_entry_ids))

    def test_no_baseline_returns_undefined_growth_not_infinity(self):
        future=self.compare(current_from=date(2026,11,1),current_through=date(2026,11,30))
        self.assertIsNone(future.revenue.percent_change)
        self.assertEqual(future.current.revenue,Decimal(0))

    def test_zero_net_profit_not_a_loss(self):
        inputs=[replace(self.entries[2],lines=(
            JournalLine("cash",debit=Decimal(90)),
            JournalLine("sales",credit=Decimal(90))))
        ]
        result=self.compare(entries=self.entries[:2]+inputs+self.entries[3:])
        self.assertNotIn("NET_LOSS",[x.code for x in result.flags])

    def test_reject_nondecimal_thresholds(self):
        for invalid in (25.0,Decimal("NaN"),Decimal("-1")):
            with self.assertRaises(AccountingError):
                self.compare(expense_growth_alert_pct=invalid)

    def test_permissions_reject_foreign_and_inactive(self):
        with self.assertRaises(AccountingError):
            self.compare(actor_id=uuid4())
        with self.assertRaises(AccountingError):
            self.compare(memberships=(Membership(self.actor,self.tenant,Role.VIEWER,False),))

    def test_cross_tenant_future_rows_also_rejected(self):
        with self.assertRaises(AccountingError):
            self.compare(entries=self.entries+[replace(
                self.entries[0],id=uuid4(),tenant_id=uuid4(),
                occurred_on=date(2027,1,1))])

    def test_imbalanced_source_not_used_for_insights(self):
        corrupt=replace(self.entries[0],lines=(
            JournalLine("cash",debit=Decimal(199)),
            JournalLine("sales",credit=Decimal(200))))
        with self.assertRaises(AccountingError):
            self.compare(entries=[corrupt]+self.entries[1:])

    def test_no_arbitrary_accusations_for_normal_results(self):
        r=self.compare(revenue_drop_alert_pct=Decimal("90"),
                       expense_growth_alert_pct=Decimal("90"))
        self.assertEqual(r.flags,())


if __name__=="__main__":
    unittest.main()
