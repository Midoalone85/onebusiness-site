import unittest
from dataclasses import replace
from datetime import date
from decimal import Decimal
from uuid import uuid4

from one_erp_core.accounting import (
    Account, AccountKind, AccountingError, AccountingPeriod, EntryStatus,
)
from one_erp_core.invoicing import (
    DocumentType, TaxCategory, InvoiceLine, InvoiceDocument,
    InvoiceLedgerAccounts, calculate_invoice, propose_invoice_journal,
)


class InvoicePolicyTests(unittest.TestCase):
    def setUp(self):
        self.tenant = uuid4()
        self.chart = {
            name: Account(name, self.tenant, code, name, kind)
            for name, code, kind in (
                ("ar", "120", AccountKind.ASSET),
                ("ap", "220", AccountKind.LIABILITY),
                ("revenue", "400", AccountKind.REVENUE),
                ("expense", "500", AccountKind.EXPENSE),
                ("vat_output", "230", AccountKind.LIABILITY),
                ("vat_input", "130", AccountKind.ASSET),
            )
        }
        self.mapping = InvoiceLedgerAccounts("ar","ap","vat_output","vat_input")
        self.period = AccountingPeriod(self.tenant,date(2026,1,1),date(2027,1,1))
        self.standard = InvoiceLine(
            "Services", "revenue", Decimal("2"), Decimal("100.00"),
            tax_category=TaxCategory.STANDARD, tax_rate=Decimal("0.15"),
            tax_code="KSA-STANDARD-EXAMPLE",
        )
        self.doc = InvoiceDocument(
            uuid4(), self.tenant, "INV-0001", "CUSTOMER-1",
            DocumentType.CUSTOMER_INVOICE,
            date(2026,10,15), date(2026,11,15), "SAR", (self.standard,),
        )

    def propose(self, doc=None):
        return propose_invoice_journal(doc or self.doc, accounts=self.mapping,
                                       chart=self.chart, period=self.period)

    def test_customer_invoice_totals_and_balanced_gl(self):
        p=self.propose()
        self.assertEqual((p.totals.net,p.totals.tax,p.totals.gross),
                         (Decimal("200.00"),Decimal("30.00"),Decimal("230.00")))
        self.assertEqual(p.journal.status, EntryStatus.DRAFT)
        self.assertEqual(p.journal.idempotency_key,f"invoice:{self.doc.id}")
        self.assertEqual({l.account_id:(l.debit,l.credit) for l in p.journal.lines},
                         {"revenue":(Decimal(0),Decimal("200.00")),
                          "vat_output":(Decimal(0),Decimal("30.00")),
                          "ar":(Decimal("230.00"),Decimal(0))})

    def test_vendor_bill_with_recoverable_vat(self):
        l=replace(self.standard,account_id="expense",input_tax_recoverable=True)
        vendor=replace(self.doc,kind=DocumentType.VENDOR_BILL,lines=(l,))
        p=self.propose(vendor)
        self.assertEqual({x.account_id:(x.debit,x.credit) for x in p.journal.lines},
                         {"expense":(Decimal("200.00"),Decimal(0)),
                          "vat_input":(Decimal("30.00"),Decimal(0)),
                          "ap":(Decimal(0),Decimal("230.00"))})

    def test_vendor_bill_nonrecoverable_vat_in_expense(self):
        l=replace(self.standard,account_id="expense",input_tax_recoverable=False)
        vendor=replace(self.doc,kind=DocumentType.VENDOR_BILL,lines=(l,))
        p=self.propose(vendor)
        self.assertEqual(len(p.journal.lines),2)
        self.assertEqual(p.journal.lines[0].debit,Decimal("230.00"))

    def test_customer_credit_note_inverts_the_sides(self):
        credit=replace(self.doc,kind=DocumentType.CUSTOMER_CREDIT,
                       source_invoice_id=uuid4())
        p=self.propose(credit)
        self.assertEqual({x.account_id:(x.debit,x.credit) for x in p.journal.lines},
                         {"revenue":(Decimal("200.00"),Decimal(0)),
                          "vat_output":(Decimal("30.00"),Decimal(0)),
                          "ar":(Decimal(0),Decimal("230.00"))})

    def test_vendor_credit_inverts_both_expense_and_payable(self):
        l=replace(self.standard,account_id="expense",input_tax_recoverable=True)
        credit=replace(self.doc,kind=DocumentType.VENDOR_CREDIT,
                       lines=(l,),source_invoice_id=uuid4())
        p=self.propose(credit)
        self.assertEqual({x.account_id:(x.debit,x.credit) for x in p.journal.lines},
                         {"expense":(Decimal(0),Decimal("200.00")),
                          "vat_input":(Decimal(0),Decimal("30.00")),
                          "ap":(Decimal("230.00"),Decimal(0))})

    def test_line_rounding_is_explicit(self):
        fractional=replace(self.standard,quantity=Decimal("0.333"),
                           unit_price=Decimal("10.00"),discount_amount=Decimal("0.00"))
        d=replace(self.doc,lines=(fractional,))
        r=calculate_invoice(d)
        self.assertEqual((r.net,r.tax,r.gross),
                         (Decimal("3.33"),Decimal("0.50"),Decimal("3.83")))

    def test_tax_semantics_preserved(self):
        taxfree=replace(self.standard,tax_category=TaxCategory.ZERO_RATED,
                        tax_rate=Decimal("0"),tax_code="ZERO")
        exempt=replace(taxfree,tax_category=TaxCategory.EXEMPT,tax_code="EXEMPT")
        r1=self.propose(replace(self.doc,lines=(taxfree,)))
        r2=self.propose(replace(self.doc,lines=(exempt,)))
        self.assertEqual(r1.totals.tax,Decimal(0))
        self.assertEqual(r2.totals.tax,Decimal(0))
        self.assertNotEqual(taxfree.tax_category,exempt.tax_category)

    def test_reject_bad_credit_reference_and_future_due(self):
        with self.assertRaises(AccountingError):
            calculate_invoice(replace(self.doc,kind=DocumentType.CUSTOMER_CREDIT))
        with self.assertRaises(AccountingError):
            calculate_invoice(replace(self.doc,due_on=date(2026,10,14)))
        with self.assertRaises(AccountingError):
            calculate_invoice(replace(self.doc,source_invoice_id=self.doc.id))

    def test_reject_floats_negative_discount_and_wrong_category(self):
        for bad in (
            replace(self.standard,unit_price=100.0),
            replace(self.standard,discount_amount=Decimal("-1")),
            replace(self.standard,tax_category=TaxCategory.ZERO_RATED),
            replace(self.standard,input_tax_recoverable=True),
        ):
            with self.assertRaises(AccountingError):
                self.propose(replace(self.doc,lines=(bad,)))

    def test_reject_cross_company_and_misconfigured_control_accounts(self):
        wrong=dict(self.chart)
        wrong["ar"]=replace(wrong["ar"],tenant_id=uuid4())
        with self.assertRaises(AccountingError):
            propose_invoice_journal(self.doc,accounts=self.mapping,
                                    chart=wrong,period=self.period)
        with self.assertRaises(AccountingError):
            self.propose(replace(self.doc,lines=(
                replace(self.standard,account_id="expense"),)))

    def test_discount_and_15pct_tax_in_demo(self):
        discounted=replace(self.standard,discount_amount=Decimal("20.00"))
        r=calculate_invoice(replace(self.doc,lines=(discounted,)))
        self.assertEqual((r.net,r.tax,r.gross),
                         (Decimal("180.00"),Decimal("27.00"),Decimal("207.00")))

    def test_no_posting_or_external_side_effect(self):
        p=self.propose()
        self.assertEqual(p.document,self.doc)
        self.assertEqual(p.journal.status,EntryStatus.DRAFT)


if __name__=="__main__":
    unittest.main()
