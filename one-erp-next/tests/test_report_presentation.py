"""Regression suite for ONE ERP static finance report output."""
import unittest
from dataclasses import replace
from datetime import date
from decimal import Decimal
from uuid import uuid4

from one_erp_core.accounting import (
    Account, AccountKind, AccountingError,
    EntryStatus, JournalEntry, JournalLine,
)
from one_erp_core.report_presentation import render_balance_sheet_html
from one_erp_core.statement_details import balance_sheet
from one_erp_core.tenancy import Membership, Role


class ReportPresentationTests(unittest.TestCase):
    def setUp(self):
        self.tenant,self.actor = uuid4(),uuid4()
        self.accounts = {
            "cash": Account("cash",self.tenant,"100","Bank <reserve>",AccountKind.ASSET),
            "equity": Account("equity",self.tenant,"300","Owner's Equity",AccountKind.EQUITY),
        }
        self.posted=JournalEntry(
            uuid4(),self.tenant,"OPEN",date(2026,10,1),"SAR","open-1",
            (JournalLine("cash",debit=Decimal("1234.50")),
             JournalLine("equity",credit=Decimal("1234.50"))),
            EntryStatus.POSTED,
        )
        self.statement = balance_sheet(
            tenant_id=self.tenant,as_of=date(2026,10,15),currency="SAR",
            chart=self.accounts,entries=[self.posted],actor_id=self.actor,
            memberships=(Membership(self.actor,self.tenant,Role.VIEWER),),
        )

    def test_arabic_rtl_print_layout_and_visible_totals(self):
        page=render_balance_sheet_html(self.statement,company_name="شركة ONE ERP")
        self.assertIn('lang="ar" dir="rtl"',page)
        self.assertIn("قائمة المركز المالي",page)
        self.assertIn("1,234.50",page)
        self.assertIn("print",page)
        self.assertIn("A4",page)
        self.assertIn("ONE ERP",page)
        self.assertIn(self.statement.as_of.isoformat(),page)
        self.assertIn("SAR",page)

    def test_english_ltr_report(self):
        page=render_balance_sheet_html(self.statement,company_name="ONE ERP",locale="en")
        self.assertIn('lang="en" dir="ltr"',page)
        self.assertIn("Statement of Financial Position",page)
        self.assertIn("Total assets",page)
        self.assertIn("Balanced",page)

    def test_unsafe_company_name_and_account_name_escaped(self):
        page=render_balance_sheet_html(
            self.statement,company_name='<img src=x onerror=alert(1)>'
        )
        self.assertNotIn("<img",page)
        self.assertIn("&lt;img",page)
        self.assertIn("Bank &lt;reserve&gt;",page)
        self.assertNotIn("Bank <reserve>",page)

    def test_not_balanced_is_visibly_flagged(self):
        inconsistent=replace(
            self.statement,total_liabilities=Decimal("10"),
            accounting_equation_difference=Decimal("-10"),balanced=False,
        )
        page=render_balance_sheet_html(inconsistent,company_name="ONE ERP",locale="en")
        self.assertIn("Out of balance",page)
        self.assertIn('status fail',page)

    def test_refuse_falsified_reassuring_status(self):
        report=replace(self.statement,total_assets=Decimal("100"))
        with self.assertRaises(AccountingError):
            render_balance_sheet_html(report,company_name="ONE ERP")

    def test_reject_invalid_locale_missing_company_and_precision(self):
        with self.assertRaises(AccountingError):
            render_balance_sheet_html(self.statement,company_name="  ")
        with self.assertRaises(AccountingError):
            render_balance_sheet_html(self.statement,company_name="ONE ERP",locale="xx")
        with self.assertRaises(AccountingError):
            render_balance_sheet_html(self.statement,company_name="ONE ERP",minor_units=7)

    def test_report_source_count_and_noindex(self):
        page=render_balance_sheet_html(self.statement,company_name="ONE ERP",locale="en")
        self.assertIn("Source journals",page)
        self.assertIn("noindex,nofollow,noarchive",page)
        self.assertIn('class="source-count"',page)


if __name__ == "__main__":
    unittest.main()
