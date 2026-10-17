import unittest
from dataclasses import replace
from datetime import date
from decimal import Decimal
from uuid import uuid4

from one_erp_core.accounting import AccountingError
from one_erp_core.reconciliation import BankMovement, LedgerMovement, propose_bank_matches


class BankMatchingTests(unittest.TestCase):
    def setUp(self):
        self.tenant=uuid4()
        self.bank=BankMovement("BANK-1",self.tenant,date(2026,10,15),"SAR",
                               Decimal("4519.50"),"SNB Transfer INV 25")
        self.ledger=[
            LedgerMovement("L-1",self.tenant,date(2026,10,15),"SAR",
                           Decimal("4519.50"),"SNB transfer INV25"),
            LedgerMovement("L-2",self.tenant,date(2026,10,14),"SAR",
                           Decimal("4519.50"),"Other receipt"),
            LedgerMovement("L-3",self.tenant,date(2026,10,15),"USD",
                           Decimal("4519.50"),"SNB transfer INV25"),
        ]

    def test_best_exact_reference_and_ambiguity(self):
        result=propose_bank_matches(self.bank,ledger=self.ledger)
        self.assertTrue(result.requires_approval)
        self.assertTrue(result.ambiguous)
        self.assertEqual([c.ledger_id for c in result.candidates],["L-1","L-2"])
        self.assertEqual(result.candidates[0].score,100)
        self.assertIn("exact normalized reference",result.candidates[0].rationale)

    def test_posted_and_unreconciled_only(self):
        movements=[replace(self.ledger[0],is_posted=False),
                   replace(self.ledger[1],is_reconciled=True)]
        result=propose_bank_matches(self.bank,ledger=movements)
        self.assertEqual(result.candidates,())
        self.assertFalse(result.ambiguous)

    def test_no_cross_tenant_leak(self):
        foreign=replace(self.ledger[0],tenant_id=uuid4())
        with self.assertRaises(AccountingError):
            propose_bank_matches(self.bank,ledger=[foreign])

    def test_wrong_sign_and_outside_date_not_matched(self):
        ledger=[
            replace(self.ledger[0],amount=Decimal("-4519.50")),
            replace(self.ledger[1],occurred_on=date(2026,10,1)),
        ]
        result=propose_bank_matches(self.bank,ledger=ledger)
        self.assertEqual(result.candidates,())

    def test_duplicate_source_and_nondecimal_rejected(self):
        with self.assertRaises(AccountingError):
            propose_bank_matches(self.bank,ledger=[self.ledger[0],self.ledger[0]])
        with self.assertRaises(AccountingError):
            propose_bank_matches(replace(self.bank,amount=4519.50),ledger=[])

    def test_invalid_tolerance_and_zero_amount_rejected(self):
        with self.assertRaises(AccountingError):
            propose_bank_matches(self.bank,ledger=[],day_tolerance=40)
        with self.assertRaises(AccountingError):
            propose_bank_matches(replace(self.bank,amount=Decimal("0")),ledger=[])


if __name__ == "__main__":
    unittest.main()
