"""Read-only dashboard composition and safe private HTML smoke tests."""
import unittest
from dataclasses import replace
from datetime import date
from decimal import Decimal
from uuid import uuid4

from one_erp_core.accounting import (
    Account,AccountKind,AccountingError,EntryStatus,JournalEntry,JournalLine,
)
from one_erp_core.finance_dashboard import build_finance_dashboard
from one_erp_core.dashboard_presentation import render_finance_dashboard_html
from one_erp_core.report_repository import LedgerSnapshot
from one_erp_core.tenancy import Membership,Role


class DashboardPresentationTests(unittest.TestCase):
    def setUp(self):
        self.tenant,self.actor=uuid4(),uuid4()
        self.chart={
            "cash":Account("cash",self.tenant,"100","Bank & cash",AccountKind.ASSET),
            "sales":Account("sales",self.tenant,"400","Revenue",AccountKind.REVENUE),
            "expense":Account("expense",self.tenant,"500","Expense",AccountKind.EXPENSE),
            "equity":Account("equity",self.tenant,"300","Equity",AccountKind.EQUITY),
        }
        def entry(ref,when,*lines):
            return JournalEntry(
                uuid4(),self.tenant,ref,when,"SAR",ref,
                tuple(JournalLine(a,Decimal(str(d)),Decimal(str(c))) for a,d,c in lines),
                EntryStatus.POSTED,
            )
        self.entries=(
            entry("OPEN",date(2026,1,1),("cash",1000,0),("equity",0,1000)),
            entry("OLD-SALE",date(2026,8,15),("cash",200,0),("sales",0,200)),
            entry("OLD-EXP",date(2026,8,18),("expense",60,0),("cash",0,60)),
            entry("SALE",date(2026,9,15),("cash",100,0),("sales",0,100)),
            entry("RENT",date(2026,9,22),("expense",90,0),("cash",0,90)),
        )
        self.memberships=(Membership(self.actor,self.tenant,Role.VIEWER),)
        self.snapshot=LedgerSnapshot(
            self.tenant,self.actor,date(2026,9,30),
            self.chart,self.entries,self.memberships,
        )

    def dashboard(self,**changes):
        return build_finance_dashboard(
            changes.get("snapshot",self.snapshot),
            from_date=changes.get("from_date",date(2026,9,1)),
            currency=changes.get("currency","SAR"),
        )

    def test_consistent_panels_kpis_and_signed_profit(self):
        r=self.dashboard()
        self.assertEqual(r.balance_sheet.total_assets,Decimal("1150"))
        self.assertEqual(r.comparative_income.current.net_profit,Decimal("10"))
        self.assertEqual(r.comparative_income.previous.net_profit,Decimal("140"))
        self.assertEqual(r.journal_count,5)
        self.assertEqual(r.trial_balance.total_debit,r.trial_balance.total_credit)
        self.assertEqual(r.source_ids,tuple(e.id for e in self.entries))
        self.assertEqual(
            [f.code for f in r.comparative_income.flags],
            ["EXPENSE_RISE","REVENUE_DECLINE"],
        )

    def test_safe_arabic_english_dashboard_with_drilldown(self):
        report=self.dashboard()
        ar=render_finance_dashboard_html(
            report,company_name='شركة <script>alert(1)</script>'
        )
        self.assertIn('lang="ar" dir="rtl"',ar)
        self.assertIn("لوحة الأداء المالي",ar)
        self.assertIn("ONE ERP",ar)
        self.assertIn("A4 landscape",ar)
        self.assertIn("<details>",ar)
        self.assertIn("1,150.00",ar)
        self.assertIn("&lt;script&gt;",ar)
        self.assertNotIn("<script>",ar)
        self.assertIn("Bank &amp; cash",ar)
        en=render_finance_dashboard_html(
            report,company_name="Acme",locale="en"
        )
        self.assertIn('lang="en" dir="ltr"',en)
        self.assertIn("Finance Performance Dashboard",en)
        self.assertIn("Trial balance",en)
        self.assertIn("Revenue",en)

    def test_deny_unauthorized_view_even_if_snapshot_faked(self):
        altered=replace(self.snapshot,actor_id=uuid4())
        with self.assertRaises(AccountingError):
            self.dashboard(snapshot=altered)
        altered=replace(self.snapshot,memberships=())
        with self.assertRaises(AccountingError):
            self.dashboard(snapshot=altered)

    def test_refuse_cross_company_or_mixed_currency(self):
        altered=replace(self.snapshot,entries=self.entries+(
            replace(self.entries[0],id=uuid4(),tenant_id=uuid4()),))
        with self.assertRaises(AccountingError):
            self.dashboard(snapshot=altered)
        with self.assertRaises(AccountingError):
            self.dashboard(currency="USD")

    def test_refuse_tampered_or_wrong_state(self):
        data=self.dashboard()
        wrong=replace(data.balance_sheet,accounting_equation_difference=Decimal(1))
        with self.assertRaises(AccountingError):
            render_finance_dashboard_html(
                replace(data,balance_sheet=wrong),company_name="ONE ERP"
            )
        with self.assertRaises(AccountingError):
            self.dashboard(from_date=date(2026,10,1))

    def test_bad_locale_and_empty_company(self):
        with self.assertRaises(AccountingError):
            render_finance_dashboard_html(self.dashboard(),company_name="")
        with self.assertRaises(AccountingError):
            render_finance_dashboard_html(
                self.dashboard(),company_name="ONE ERP",locale="fr"
            )

    def test_duplicate_posted_entries_not_included_twice(self):
        altered=replace(self.snapshot,entries=self.entries+self.entries[:1])
        with self.assertRaises(AccountingError):
            self.dashboard(snapshot=altered)


if __name__ == "__main__":
    unittest.main()
