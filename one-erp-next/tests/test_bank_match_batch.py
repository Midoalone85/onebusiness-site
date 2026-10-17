"""Bank statement batch suggestions are never actual cash settlement evidence."""
from dataclasses import replace
from datetime import date,timedelta
from decimal import Decimal
import unittest
from uuid import uuid4

from one_erp_core.accounting import AccountingError
from one_erp_core.bank_match_batch import propose_bank_batch
from one_erp_core.reconciliation import BankMovement,LedgerMovement


class BankBatchReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.tenant=uuid4()
        self.bank=BankMovement(
            id="SNB-LINE-01",tenant_id=self.tenant,
            value_date=date(2026,10,17),currency="SAR",
            amount=Decimal("4519.50"),reference="Customer INV 887"
        )
        self.ledger=LedgerMovement(
            id="INTERNAL-RECEIPT-01",tenant_id=self.tenant,
            occurred_on=date(2026,10,17),currency="SAR",
            amount=Decimal("4519.50"),reference="customer inv887",
        )

    def test_single_equal_bank_receipt_is_only_a_manual_candidate(self):
        x=propose_bank_batch(self.tenant,bank_lines=[self.bank],
                             ledger_rows=[self.ledger])
        self.assertEqual(x.statement_evidence,"import_unverified")
        self.assertFalse(x.bank_settlement_verified)
        self.assertFalse(x.writes_performed)
        self.assertEqual(len(x.items),1)
        self.assertEqual(x.items[0].bank_line_id,self.bank.id)
        self.assertTrue(x.items[0].requires_independent_review)
        self.assertEqual(x.items[0].outcome,
            "unique_candidate_requires_independent_review")
        self.assertEqual(x.items[0].matching_ledger_ids,(self.ledger.id,))

    def test_duplicate_bank_movements_competing_for_one_receipt_are_flagged(self):
        second=replace(self.bank,id="SNB-LINE-02",reference="duplicate memo")
        x=propose_bank_batch(self.tenant,
            bank_lines=[self.bank,second],ledger_rows=[self.ledger])
        self.assertTrue(all(item.outcome==
            "collision_same_ledger_proposed_for_multiple_bank_lines"
            for item in x.items))
        self.assertTrue(all(item.requires_independent_review for item in x.items))

    def test_one_bank_line_and_two_matching_ledgers_cannot_be_assigned_arbitrarily(self):
        second=replace(self.ledger,id="INTERNAL-RECEIPT-02",reference="different")
        x=propose_bank_batch(self.tenant,bank_lines=[self.bank],
                             ledger_rows=[self.ledger,second])
        self.assertEqual(x.items[0].outcome,"ambiguous_multiple_ledger_candidates")
        self.assertEqual(set(x.items[0].matching_ledger_ids),
                         {self.ledger.id,second.id})

    def test_ambiguous_second_bank_still_blocks_unique_first_bank_from_claiming_ledger(self):
        other=replace(self.ledger,id="OTHER",amount=Decimal("4519.50"),
                      reference="different invoice",occurred_on=date(2026,10,15))
        second=replace(self.bank,id="SNB-LINE-02",value_date=date(2026,10,14),
                       reference="different invoice")
        x=propose_bank_batch(self.tenant,bank_lines=[self.bank,second],
                             ledger_rows=[self.ledger,other])
        self.assertEqual(x.items[0].outcome,
            "collision_same_ledger_proposed_for_multiple_bank_lines")
        self.assertEqual(x.items[1].outcome,"ambiguous_multiple_ledger_candidates")

    def test_no_cross_currency_or_fuzzy_amount_and_unposted_ledger(self):
        cases=[
            replace(self.ledger,currency="USD"),
            replace(self.ledger,amount=Decimal("4519.49")),
            replace(self.ledger,occurred_on=date(2026,9,1)),
            replace(self.ledger,is_posted=False),
            replace(self.ledger,is_reconciled=True),
        ]
        for row in cases:
            with self.subTest(row=row):
                x=propose_bank_batch(self.tenant,bank_lines=[self.bank],ledger_rows=[row])
                self.assertEqual(x.items[0].outcome,"no_candidate")

    def test_cross_tenant_invalid_precision_and_duplicate_identifiers_fail_closed(self):
        cases=[
            {"bank_lines":[replace(self.bank,tenant_id=uuid4())]},
            {"ledger_rows":[replace(self.ledger,tenant_id=uuid4())]},
            {"bank_lines":[self.bank,self.bank]},
            {"ledger_rows":[self.ledger,self.ledger]},
            {"bank_lines":[replace(self.bank,amount=Decimal("0"))]},
            {"bank_lines":[replace(self.bank,amount=Decimal("1.0001"))]},
            {"bank_lines":[replace(self.bank,currency="sar")]},
            {"bank_lines":[replace(self.bank,reference=None)]},
            {"ledger_rows":[replace(self.ledger,occurred_on=None)]},
            {"ledger_rows":[replace(self.ledger,amount=1.0)]},
        ]
        for changed in cases:
            with self.subTest(changed=changed),self.assertRaises(AccountingError):
                propose_bank_batch(self.tenant,bank_lines=[self.bank],
                    ledger_rows=[self.ledger],**changed)

    def test_large_inputs_and_malformed_day_window_refused(self):
        with self.assertRaises(AccountingError):
            propose_bank_batch(self.tenant,
                bank_lines=[replace(self.bank,id=str(i)) for i in range(201)],
                ledger_rows=[])
        for tolerance in (-1,32,1.5):
            with self.assertRaises(AccountingError):
                propose_bank_batch(self.tenant,bank_lines=[self.bank],
                                   ledger_rows=[self.ledger],day_tolerance=tolerance)

    def test_no_bank_lines_gives_empty_report_without_mutation(self):
        x=propose_bank_batch(self.tenant,bank_lines=[],ledger_rows=[self.ledger])
        self.assertEqual(x.items,())
        self.assertFalse(x.bank_settlement_verified)


if __name__=="__main__":
    unittest.main()
