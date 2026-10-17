"""KSA VAT source-consistency tests. No ZATCA API or XML calls."""
import unittest
from dataclasses import replace
from datetime import date
from decimal import Decimal as D
from uuid import uuid4

from one_erp_core.accounting import AccountingError
from one_erp_core.invoicing import (
    DocumentType, InvoiceDocument, InvoiceLine, TaxCategory,
)
from one_erp_core.ksa_credit_source_policy import (
    KSASourceNoteEvidence,assess_ksa_standard_goods_credit_source
)


class KSACreditSourceTests(unittest.TestCase):
    def setUp(self):
        self.company=uuid4()
        self.original=InvoiceDocument(
            id=uuid4(),tenant_id=self.company,number="INV-120",
            partner_id="buyer-1",kind=DocumentType.CUSTOMER_INVOICE,
            issued_on=date(2026,10,10),due_on=date(2026,10,31),
            currency="SAR",lines=(
                InvoiceLine("Goods","sales",D("5"),D("100.00"),
                    tax_category=TaxCategory.STANDARD,
                    tax_rate=D("0.15"),tax_code="VAT15"),),
        )
        self.credit=InvoiceDocument(
            id=uuid4(),tenant_id=self.company,number="CR-120",
            partner_id="buyer-1",kind=DocumentType.CUSTOMER_CREDIT,
            issued_on=date(2026,10,15),due_on=date(2026,10,15),
            currency="SAR",source_invoice_id=self.original.id,
            lines=(replace(self.original.lines[0],quantity=D("2")),),
        )
        self.evidence=KSASourceNoteEvidence(
            original_invoice_id=self.original.id,
            original_invoice_number=self.original.number,
            source_registered=True,original_fatoora_route="standard",
            original_zatca_state="cleared",
            seller_vat_number="300000000000003",
            buyer_legal_identity_confirmed=True,
            accepted_return_id=uuid4(),accepted_return_quantity=D("2"),
            earlier_credited_source_quantity=D("0"),
            approved_vat_code="VAT15",reason="goods_return",
            reason_detail="Customer returned two faulty items",
        )

    def check(self,*,original=None,credit=None,evidence=None):
        return assess_ksa_standard_goods_credit_source(
            original=original or self.original,credit=credit or self.credit,
            evidence=evidence or self.evidence)

    def test_standard_invoice_matches_reference_and_15_percent_tax(self):
        check=self.check()
        self.assertEqual(check.original_invoice_number,"INV-120")
        self.assertEqual(check.expected_document_type,"381")
        self.assertEqual(check.required_zatca_route,"standard")
        self.assertEqual(check.base_amount_sar,D("200"))
        self.assertEqual(check.output_vat_reversal_sar,D("30.00"))
        self.assertEqual(check.total_credit_sar,D("230.00"))

    def test_simplified_invoice_follows_reporting_not_clearance(self):
        c=self.check(evidence=replace(self.evidence,
            original_fatoora_route="simplified",
            original_zatca_state="reported"))
        self.assertEqual(c.required_zatca_route,"simplified")
        with self.assertRaises(AccountingError):
            self.check(evidence=replace(self.evidence,
                original_fatoora_route="simplified",
                original_zatca_state="cleared"))

    def test_previous_credited_quantity_is_accounted_for(self):
        self.check(evidence=replace(
            self.evidence,earlier_credited_source_quantity=D("3")))
        with self.assertRaises(AccountingError):
            self.check(evidence=replace(
                self.evidence,earlier_credited_source_quantity=D("4")))

    def test_missing_original_invoice_reference_fails(self):
        for evidence in (
            replace(self.evidence,original_invoice_id=uuid4()),
            replace(self.evidence,original_invoice_number="WRONG"),
            replace(self.evidence,source_registered=False),
        ):
            with self.assertRaises(AccountingError):
                self.check(evidence=evidence)

    def test_vat_registration_and_identity_required(self):
        for evidence in (
            replace(self.evidence,seller_vat_number="bad"),
            replace(self.evidence,seller_vat_number="0"*15),
            replace(self.evidence,buyer_legal_identity_confirmed=False),
            replace(self.evidence,approved_vat_code="INVALID"),
        ):
            with self.assertRaises(AccountingError):
                self.check(evidence=evidence)

    def test_tax_rate_category_and_source_price_must_match(self):
        for change in (
            {"tax_rate":D("0.05")},{"tax_category":TaxCategory.ZERO_RATED},
            {"tax_code":"OTHER"},{"unit_price":D("99")},
            {"discount_amount":D("5")},
            {"account_id":"unmapped"},
        ):
            with self.assertRaises(AccountingError):
                self.check(credit=replace(
                    self.credit,lines=(replace(self.credit.lines[0],**change),)))

    def test_wrong_buyer_tenant_currency_and_date_refused(self):
        for credit in (
            replace(self.credit,partner_id="buyer-2"),
            replace(self.credit,tenant_id=uuid4()),
            replace(self.credit,currency="USD"),
            replace(self.credit,issued_on=date(2026,10,1)),
            replace(self.credit,source_invoice_id=uuid4()),
        ):
            with self.assertRaises(AccountingError):
                self.check(credit=credit)

    def test_source_issuance_and_return_reason_required(self):
        for evidence in (
            replace(self.evidence,reason="other"),
            replace(self.evidence,reason_detail="x"),
            replace(self.evidence,original_zatca_state="reported"),
        ):
            with self.assertRaises(AccountingError):
                self.check(evidence=evidence)

    def test_accepted_return_quantity_cannot_be_faked_or_float(self):
        for quantity in (D("1"),D("6"),D("1.0001"),D("NaN"),2.0):
            with self.assertRaises(AccountingError):
                self.check(evidence=replace(
                    self.evidence,accepted_return_quantity=quantity))

    def test_overcredit_different_document_kind_or_source_discount_rejected(self):
        with self.assertRaises(AccountingError):
            self.check(credit=replace(self.credit,
                kind=DocumentType.VENDOR_CREDIT))
        with self.assertRaises(AccountingError):
            self.check(original=replace(self.original,lines=(
                replace(self.original.lines[0],discount_amount=D("1")),)))
        with self.assertRaises(AccountingError):
            self.check(credit=replace(self.credit,lines=(
                replace(self.credit.lines[0],quantity=D("5")),)))


if __name__ == "__main__":
    unittest.main()
