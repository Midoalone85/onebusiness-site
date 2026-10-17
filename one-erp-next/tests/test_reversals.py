import unittest
from dataclasses import replace
from datetime import date
from decimal import Decimal
from uuid import uuid4

from one_erp_core.accounting import (
    AccountingError, Account, AccountKind, AccountingPeriod,
    EntryStatus, JournalEntry, JournalLine, validate_entry,
)
from one_erp_core.reversals import make_reversal


class ReversalsTest(unittest.TestCase):
    def setUp(self):
        self.tenant=uuid4()
        self.original=JournalEntry(
            uuid4(),self.tenant,"INV001",date(2026,1,15),"SAR","invo1",
            (JournalLine("cash",Decimal("100.00")),
             JournalLine("revenue",credit=Decimal("100.00"))),
            EntryStatus.POSTED
        )
        self.chart={
            "cash":Account("cash",self.tenant,"100","Cash",AccountKind.ASSET),
            "revenue":Account("revenue",self.tenant,"400","Revenue",AccountKind.REVENUE),
        }

    def test_exact_reversal_and_original_remains_intact(self):
        before=self.original
        reversal=make_reversal(self.original,reversal_on=date(2026,1,16),reason="Wrong customer")
        self.assertEqual(self.original,before)
        self.assertEqual(reversal.status,EntryStatus.DRAFT)
        self.assertEqual(reversal.reversal_of,self.original.id)
        self.assertEqual(reversal.idempotency_key,f"reversal:{self.original.id}")
        self.assertEqual(reversal.lines[0].credit,Decimal("100.00"))
        self.assertEqual(reversal.lines[1].debit,Decimal("100.00"))
        validate_entry(reversal,chart=self.chart,
            period=AccountingPeriod(self.tenant,date(2026,1,1),date(2027,1,1)))

    def test_cannot_reverse_draft_reversal_or_backdate(self):
        draft=replace(self.original,status=EntryStatus.DRAFT)
        with self.assertRaises(AccountingError):
            make_reversal(draft,reversal_on=date(2026,1,16),reason="Correction")
        reversal=make_reversal(self.original,reversal_on=date(2026,1,16),reason="Correction")
        with self.assertRaises(AccountingError):
            make_reversal(replace(reversal,status=EntryStatus.POSTED),
                reversal_on=date(2026,1,17),reason="Further correction")
        with self.assertRaises(AccountingError):
            make_reversal(self.original,reversal_on=date(2026,1,14),reason="Old date")

    def test_reversal_requires_reason_and_only_once(self):
        with self.assertRaises(AccountingError):
            make_reversal(self.original,reversal_on=date(2026,1,16),reason="   ")
        r=make_reversal(self.original,reversal_on=date(2026,1,16),reason="Reason")
        with self.assertRaises(AccountingError):
            make_reversal(self.original,reversal_on=date(2026,1,17),
                reason="Again",existing_reversals=(r,))

    def test_self_reversal_forbidden_in_ledger_policy(self):
        entry=replace(self.original,status=EntryStatus.DRAFT,
                      reversal_of=self.original.id)
        with self.assertRaises(AccountingError):
            validate_entry(entry,chart=self.chart,
                period=AccountingPeriod(self.tenant,date(2026,1,1),date(2027,1,1)))


if __name__ == "__main__":
    unittest.main()
