"""Permission and accounting-integrity regressions for financial statements."""
import unittest
from dataclasses import replace
from datetime import date
from decimal import Decimal
from uuid import uuid4

from one_erp_core.accounting import (
    Account, AccountKind, AccountingError, EntryStatus, JournalEntry, JournalLine,
)
from one_erp_core.statement_details import balance_sheet, general_ledger_detail
from one_erp_core.tenancy import Membership, Role


class SecureFinancialStatementsTests(unittest.TestCase):
    def setUp(self):
        self.tenant, self.actor = uuid4(), uuid4()
        self.chart = {
            name: Account(name, self.tenant, code, name, kind)
            for name,code,kind in (
                ("bank","100",AccountKind.ASSET),
                ("ar","110",AccountKind.ASSET),
                ("ap","200",AccountKind.LIABILITY),
                ("vat","210",AccountKind.LIABILITY),
                ("equity","300",AccountKind.EQUITY),
                ("sales","400",AccountKind.REVENUE),
                ("rent","500",AccountKind.EXPENSE),
            )
        }
        def posted(reference, when, *lines):
            return JournalEntry(
                uuid4(), self.tenant,reference,when,"SAR",reference,
                tuple(JournalLine(name,Decimal(str(d)),Decimal(str(c))) for name,d,c in lines),
                EntryStatus.POSTED,
            )
        self.opening=posted("OPEN",date(2026,1,1),
                           ("bank",500,0),("equity",0,500))
        self.sale=posted("SALE",date(2026,4,1),
                         ("ar",115,0),("sales",0,100),("vat",0,15))
        self.cost=posted("RENT",date(2026,4,5),
                         ("rent",30,0),("ap",0,30))
        self.entries=[self.opening,self.sale,self.cost]
        self.memberships=(Membership(self.actor,self.tenant,Role.VIEWER),)

    def sheet(self,**kwargs):
        args=dict(tenant_id=self.tenant,as_of=date(2026,10,15),
                  currency="SAR",chart=self.chart,entries=self.entries,
                  actor_id=self.actor,memberships=self.memberships)
        args.update(kwargs)
        return balance_sheet(**args)

    def ledger(self,**kwargs):
        args=dict(tenant_id=self.tenant,account_id="bank",
                  from_date=date(2026,3,1),through_date=date(2026,10,15),
                  currency="SAR",chart=self.chart,entries=self.entries,
                  actor_id=self.actor,memberships=self.memberships)
        args.update(kwargs)
        return general_ledger_detail(**args)

    def test_real_accounting_equation_and_unclosed_income(self):
        r=self.sheet()
        self.assertEqual((r.total_assets,r.total_liabilities,
                          r.contributed_and_retained_equity,
                          r.unclosed_profit_to_date),
                         (Decimal(615),Decimal(45),Decimal(500),Decimal(70)))
        self.assertEqual(r.total_equity_including_profit,Decimal(570))
        self.assertTrue(r.balanced)
        self.assertEqual(r.accounting_equation_difference,Decimal(0))
        self.assertEqual(set(r.source_entry_ids),{e.id for e in self.entries})

    def test_lineage_attached_to_each_account(self):
        r=self.sheet()
        ar=next(x for x in r.assets if x.account_id=="ar")
        self.assertEqual(ar.amount,Decimal(115))
        self.assertEqual(ar.source_entry_ids,(self.sale.id,))
        liability=next(x for x in r.liabilities if x.account_id=="vat")
        self.assertEqual(liability.amount,Decimal(15))

    def test_viewer_can_read_but_cannot_use_foreign_membership(self):
        self.assertTrue(self.sheet().balanced)
        with self.assertRaises(AccountingError):
            self.sheet(actor_id=uuid4())
        with self.assertRaises(AccountingError):
            self.sheet(memberships=(Membership(self.actor,uuid4(),Role.COMPANY_ADMIN),))
        with self.assertRaises(AccountingError):
            self.sheet(memberships=(Membership(self.actor,self.tenant,Role.VIEWER,False),))

    def test_cross_tenant_data_rejected_before_date_filters(self):
        other=replace(self.sale,tenant_id=uuid4(),occurred_on=date(2027,1,1))
        with self.assertRaises(AccountingError):
            self.sheet(entries=[self.opening,other])
        foreign=dict(self.chart)
        foreign["ar"]=replace(foreign["ar"],tenant_id=uuid4())
        with self.assertRaises(AccountingError):
            self.sheet(chart=foreign)

    def test_corrupted_journal_does_not_make_fake_statement(self):
        wrong=replace(self.sale,lines=(
            JournalLine("ar",debit=Decimal(114)),
            JournalLine("sales",credit=Decimal(100)),
            JournalLine("vat",credit=Decimal(15)),
        ))
        with self.assertRaises(AccountingError):
            self.sheet(entries=[self.opening,wrong])

    def test_float_nan_and_two_sided_lines_rejected(self):
        for line in (
            JournalLine("ar",debit=115.0),
            JournalLine("ar",debit=Decimal("NaN")),
            JournalLine("ar",debit=Decimal(115),credit=Decimal(1)),
        ):
            tampered=replace(self.sale,lines=(line,)+self.sale.lines[1:])
            with self.assertRaises(AccountingError):
                self.sheet(entries=[self.opening,tampered])

    def test_duplicate_posted_id_refused(self):
        with self.assertRaises(AccountingError):
            self.sheet(entries=self.entries+[self.sale])

    def test_drafts_excluded_including_future_date(self):
        draft=replace(self.sale,id=uuid4(),status=EntryStatus.DRAFT)
        future=replace(self.sale,id=uuid4(),occurred_on=date(2026,12,1))
        result=self.sheet(entries=self.entries+[draft,future])
        self.assertEqual(result.total_assets,Decimal(615))
        self.assertEqual(len(result.source_entry_ids),3)

    def test_currency_mix_requires_explicit_fx(self):
        mixed=replace(self.sale,currency="USD")
        with self.assertRaises(AccountingError):
            self.sheet(entries=[self.opening,mixed])

    def test_gl_opening_movement_period_and_source(self):
        r=self.ledger(account_id="ar",from_date=date(2026,4,2))
        self.assertEqual(r.opening_debit_minus_credit,Decimal(115))
        self.assertEqual(r.movements,())
        self.assertEqual(r.closing_debit_minus_credit,Decimal(115))
        r=self.ledger(account_id="ar",from_date=date(2026,4,1))
        self.assertEqual(r.opening_debit_minus_credit,Decimal(0))
        self.assertEqual(r.total_period_debit,Decimal(115))
        self.assertEqual(r.closing_debit_minus_credit,Decimal(115))
        self.assertEqual(r.movements[0].reference,"SALE")
        self.assertEqual(r.movements[0].journal_id,self.sale.id)
        self.assertEqual(r.source_entry_ids,(self.sale.id,))

    def test_gl_access_dates_and_unknown_account(self):
        with self.assertRaises(AccountingError):
            self.ledger(from_date=date(2026,12,1),through_date=date(2026,10,1))
        with self.assertRaises(AccountingError):
            self.ledger(account_id="not-a-real-account")
        with self.assertRaises(AccountingError):
            self.ledger(memberships=())
        with self.assertRaises(AccountingError):
            self.ledger(entries=[replace(self.cost,tenant_id=uuid4())])

    def test_negative_natural_balance_not_hidden(self):
        # Accounting credits can generate a negative 'normal' asset balance;
        # expose rather than silently taking abs or forcing nonnegative.
        a=JournalEntry(uuid4(),self.tenant,"NEG",date(2026,4,6),"SAR","NEG",
                       (JournalLine("equity",debit=Decimal(600)),
                        JournalLine("bank",credit=Decimal(600))),EntryStatus.POSTED)
        r=self.sheet(entries=self.entries+[a])
        bank=next(x for x in r.assets if x.account_id=="bank")
        self.assertEqual(bank.amount,Decimal(-100))
        self.assertTrue(r.balanced)


if __name__ == "__main__":
    unittest.main()
