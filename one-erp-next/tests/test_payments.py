import unittest
from dataclasses import replace
from datetime import date
from decimal import Decimal
from uuid import uuid4

from one_erp_core.accounting import (
    Account, AccountKind, AccountingError, AccountingPeriod, EntryStatus,
)
from one_erp_core.payments import (
    OpenInvoice, PaymentAllocation, PaymentDirection, PaymentLedgerAccounts,
    PaymentRequest, propose_payment_journal,
)


class PaymentPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tenant,self.user=uuid4(),uuid4()
        self.chart={
            name:Account(name,self.tenant,code,name,kind)
            for name,code,kind in (
                ("bank","110",AccountKind.ASSET),
                ("ar","120",AccountKind.ASSET),
                ("ap","220",AccountKind.LIABILITY),
            )
        }
        self.accounts=PaymentLedgerAccounts("bank","ar","ap")
        self.period=AccountingPeriod(self.tenant,date(2026,1,1),date(2027,1,1))
        self.invoice=OpenInvoice(uuid4(),self.tenant,"party1","SAR",
                                 date(2026,10,1),Decimal("1000.00"),True)
        self.other=OpenInvoice(uuid4(),self.tenant,"party1","SAR",
                               date(2026,10,2),Decimal("750.00"),True)
        self.payment=PaymentRequest(
            uuid4(),self.tenant,"party1",date(2026,10,15),"SAR",
            PaymentDirection.CUSTOMER_RECEIPT,Decimal("500.00"),
            (PaymentAllocation(self.invoice.id,Decimal("500.00")),),"BANK-REF-01",
        )

    def propose(self,payment=None,source=None,chart=None):
        return propose_payment_journal(
            payment or self.payment,
            invoices=(self.invoice,self.other) if source is None else source,
            accounts=self.accounts,chart=self.chart if chart is None else chart,
            period=self.period,
        )

    def test_customer_receipt_debits_bank_credits_ar(self):
        p=self.propose()
        self.assertEqual(p.allocated_total,Decimal("500.00"))
        self.assertEqual(p.journal.status,EntryStatus.DRAFT)
        self.assertEqual(p.journal.idempotency_key,f"payment:{self.payment.id}")
        self.assertEqual(p.journal.lines[0].debit,Decimal("500.00"))
        self.assertEqual(p.journal.lines[1].credit,Decimal("500.00"))

    def test_vendor_payment_debits_ap_credits_bank(self):
        pay=replace(self.payment,direction=PaymentDirection.VENDOR_DISBURSEMENT)
        p=self.propose(pay)
        self.assertEqual(p.journal.lines[0].account_id,"ap")
        self.assertEqual(p.journal.lines[0].debit,Decimal("500.00"))
        self.assertEqual(p.journal.lines[1].account_id,"bank")
        self.assertEqual(p.journal.lines[1].credit,Decimal("500.00"))

    def test_split_payment_exact_total(self):
        pay=replace(self.payment,amount=Decimal("800.00"),allocations=(
            PaymentAllocation(self.invoice.id,Decimal("500.00")),
            PaymentAllocation(self.other.id,Decimal("300.00")),
        ))
        self.assertEqual(self.propose(pay).allocated_total,Decimal("800.00"))

    def test_overpay_and_wrong_total_fail(self):
        for pay in (
            replace(self.payment,amount=Decimal("1200.00"),allocations=(
                PaymentAllocation(self.invoice.id,Decimal("1200.00")),)),
            replace(self.payment,amount=Decimal("600.00")),
        ):
            with self.assertRaises(AccountingError):
                self.propose(pay)

    def test_reject_duplicate_allocations(self):
        pay=replace(self.payment,amount=Decimal("1000.00"),allocations=(
            PaymentAllocation(self.invoice.id,Decimal("500.00")),
            PaymentAllocation(self.invoice.id,Decimal("500.00")),
        ))
        with self.assertRaises(AccountingError):
            self.propose(pay)

    def test_tenant_and_partner_currency_safety(self):
        with self.assertRaises(AccountingError):
            self.propose(source=(replace(self.invoice,tenant_id=uuid4()),))
        for inv in (replace(self.invoice,currency="USD"),
                    replace(self.invoice,partner_id="another"),
                    replace(self.invoice,is_posted=False),
                    replace(self.invoice,is_cancelled=True)):
            with self.assertRaises(AccountingError):
                self.propose(source=(inv,))

    def test_precision_and_date_checks(self):
        for amount in (500.0,Decimal("-500"),Decimal("500.001")):
            with self.assertRaises(AccountingError):
                self.propose(replace(self.payment,amount=amount))
        with self.assertRaises(AccountingError):
            self.propose(replace(self.payment,occurred_on=date(2026,9,1)))

    def test_invalid_bank_account_rejected(self):
        chart=dict(self.chart,bank=replace(self.chart["bank"],kind=AccountKind.EXPENSE))
        with self.assertRaises(AccountingError):
            self.propose(chart=chart)


if __name__=="__main__":
    unittest.main()
