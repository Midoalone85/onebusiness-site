import unittest
from dataclasses import replace
from datetime import date
from decimal import Decimal
from uuid import uuid4

from one_erp_core.accounting import AccountingError
from one_erp_core.aging_reports import (
    IssuedInvoiceSnapshot,AppliedPaymentSnapshot,aging_report,
)
from one_erp_core.invoicing import DocumentType


class AgingReportTests(unittest.TestCase):
    def setUp(self):
        self.tenant=uuid4()
        self.invoice=IssuedInvoiceSnapshot(
            uuid4(),self.tenant,"INV-1","PARTY-1",
            DocumentType.CUSTOMER_INVOICE,date(2026,1,1),date(2026,1,31),
            "SAR",Decimal("115.00"),
        )
        self.credit=IssuedInvoiceSnapshot(
            uuid4(),self.tenant,"CN-1","PARTY-1",DocumentType.CUSTOMER_CREDIT,
            date(2026,2,2),date(2026,2,2),"SAR",Decimal("15.00"),self.invoice.id,
        )
        self.payment=AppliedPaymentSnapshot(
            uuid4(),self.tenant,self.invoice.id,date(2026,2,5),
            "SAR",Decimal("25.00"),
        )

    def report(self,*,as_of=date(2026,3,4),invoices=None,payments=None):
        return aging_report(
            tenant_id=self.tenant,as_of=as_of,kind=DocumentType.CUSTOMER_INVOICE,
            currency="SAR",
            invoices=(self.invoice,self.credit) if invoices is None else invoices,
            payments=(self.payment,) if payments is None else payments,
        )

    def test_aging_and_trace_ids(self):
        r=self.report()
        self.assertEqual(r.grand_total,Decimal("75.00"))
        self.assertEqual(r.rows[0].bucket,"31-60")
        self.assertEqual(r.rows[0].days_overdue,32)
        self.assertEqual(set(r.rows[0].source_ids),
                         {self.invoice.id,self.credit.id,self.payment.payment_id})
        self.assertEqual(r.totals["31-60"],Decimal("75.00"))

    def test_payment_and_credit_after_asof_excluded(self):
        r=self.report(as_of=date(2026,1,31))
        self.assertEqual(r.grand_total,Decimal("115.00"))
        self.assertEqual(r.rows[0].bucket,"current")

    def test_paid_off_documents_not_shown(self):
        pay=replace(self.payment,amount=Decimal("100.00"))
        r=self.report(payments=(pay,))
        self.assertEqual(r.grand_total,Decimal("0"))
        self.assertEqual(r.rows,())

    def test_overpayment_cannot_be_silently_hidden(self):
        pay=replace(self.payment,amount=Decimal("110.00"))
        with self.assertRaises(AccountingError):
            self.report(payments=(pay,))

    def test_mixed_tenants_and_currencies_rejected(self):
        for item in (replace(self.credit,tenant_id=uuid4()),
                     replace(self.credit,currency="USD")):
            with self.assertRaises(AccountingError):
                self.report(invoices=(self.invoice,item))

    def test_vendor_aging(self):
        bill=replace(self.invoice,kind=DocumentType.VENDOR_BILL)
        credit=replace(self.credit,kind=DocumentType.VENDOR_CREDIT)
        report=aging_report(
            tenant_id=self.tenant,as_of=date(2026,3,4),
            kind=DocumentType.VENDOR_BILL,currency="SAR",
            invoices=(bill,credit),payments=(self.payment,),
        )
        self.assertEqual(report.grand_total,Decimal("75.00"))


if __name__=="__main__":
    unittest.main()
