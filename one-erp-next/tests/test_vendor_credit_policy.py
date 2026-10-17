"""Safe preflight checks: billed supplier credits do not mutate the ledger."""
import unittest
from dataclasses import replace
from datetime import date
from decimal import Decimal as D
from uuid import uuid4

from one_erp_core.accounting import AccountingError
from one_erp_core.invoicing import (
    DocumentType, TaxCategory, InvoiceLine, InvoiceDocument
)
from one_erp_core.vendor_credit_policy import (
    BilledVendorReturnSource, propose_billed_vendor_credit
)


class BilledVendorCreditPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tenant, self.bill, self.job, self.receipt = (uuid4() for _ in range(4))
        self.source = BilledVendorReturnSource(
            tenant_id=self.tenant, return_job_id=self.job,
            source_receipt_id=self.receipt, original_bill_id=self.bill,
            supplier_id="supplier-1", currency="SAR", return_status="requested",
            requested_quantity=D("2"), receipt_quantity=D("5"),
            receipt_cost=D("62.50"), previously_returned_from_receipt=D("0"),
            po_line_billed_quantity=D("5"), previously_billed_returned_po_line=D("0"),
            original_bill_line_quantity=D("5"),
            previously_credited_bill_line_quantity=D("0"),
            available_unreserved_quantity=D("5"), position_quantity=D("5"),
            position_book_value=D("62.50"), position_currency="SAR",
            original_unit_price=D("12.50"),
            original_tax_category=TaxCategory.OUT_OF_SCOPE,
            original_tax_rate=D("0"), original_tax_code="NONE",
            original_input_tax_recoverable=False, original_bill_issued=True,
            original_receipt_valued=True, source_bill_line_allocated_to_po=True,
        )
        self.credit = InvoiceDocument(
            id=uuid4(), tenant_id=self.tenant, number="VC-1",
            partner_id="supplier-1", kind=DocumentType.VENDOR_CREDIT,
            issued_on=date(2026, 10, 15), due_on=date(2026, 10, 15),
            currency="SAR", source_invoice_id=self.bill,
            lines=(InvoiceLine("Returned part", "INVENTORY", D("2"), D("12.50")),),
        )

    def check_refused(self, *, source=None, doc=None, line=None):
        old = replace(self.source, **(source or {}))
        new = replace(self.credit, **(doc or {}))
        if line:
            new = replace(new, lines=(replace(new.lines[0], **line),))
        with self.assertRaises(AccountingError):
            propose_billed_vendor_credit(source=old, credit=new)

    def test_two_valued_billed_units_reverse_ap_at_original_cost(self):
        proposal = propose_billed_vendor_credit(source=self.source, credit=self.credit)
        self.assertEqual(proposal.debit_accounts_payable, D("25.00"))
        self.assertEqual(proposal.credit_inventory, D("25.00"))
        self.assertEqual(proposal.inventory_quantity_after, D("3"))
        self.assertEqual(proposal.inventory_value_after, D("37.50"))

    def test_partial_source_return_and_partial_supplier_credit(self):
        prior = replace(
            self.source, previously_returned_from_receipt=D("1"),
            previously_billed_returned_po_line=D("2"),
            previously_credited_bill_line_quantity=D("2"))
        self.assertEqual(
            propose_billed_vendor_credit(source=prior, credit=self.credit).quantity,
            D("2"))

    def test_prevent_duplicate_bill_credit(self):
        self.check_refused(source={"previously_credited_bill_line_quantity": D("4")})

    def test_prevent_duplicate_physical_source_return(self):
        self.check_refused(source={"previously_returned_from_receipt": D("4")})

    def test_prevent_more_billed_returns_than_vendor_bill(self):
        self.check_refused(source={"po_line_billed_quantity": D("3"),
                                   "previously_billed_returned_po_line": D("2")})

    def test_reserved_goods_cannot_return(self):
        self.check_refused(source={"available_unreserved_quantity": D("1")})

    def test_company_tenant_must_match(self):
        self.check_refused(doc={"tenant_id": uuid4()})

    def test_credit_must_reference_original_bill(self):
        self.check_refused(doc={"source_invoice_id": uuid4()})

    def test_supplier_and_currency_must_match(self):
        self.check_refused(doc={"partner_id": "other"})
        self.check_refused(doc={"currency": "USD"})
        self.check_refused(source={"position_currency": "USD"})

    def test_issued_bill_and_valued_receipt_required(self):
        self.check_refused(source={"original_bill_issued": False})
        self.check_refused(source={"original_receipt_valued": False})
        self.check_refused(source={"source_bill_line_allocated_to_po": False})

    def test_discount_and_vat_are_not_silently_accepted(self):
        self.check_refused(line={"discount_amount": D("1")})
        self.check_refused(line={"tax_rate": D("0.15")})
        self.check_refused(line={"input_tax_recoverable": True})
        self.check_refused(source={"original_tax_rate": D("0.15")})

    def test_tax_snapshot_and_code_must_match(self):
        self.check_refused(line={"tax_category": TaxCategory.STANDARD})
        self.check_refused(line={"tax_code": "VAT15"})

    def test_no_unapproved_cost_variance(self):
        self.check_refused(source={"receipt_cost": D("65")})
        self.check_refused(source={"position_book_value": D("75")})
        self.check_refused(line={"unit_price": D("12.60")})

    def test_quantity_is_strictly_three_decimal_places(self):
        self.check_refused(line={"quantity": D("1.0001")})
        self.check_refused(line={"quantity": D("NaN")})
        self.check_refused(line={"quantity": D("0")})

    def test_requested_return_must_be_open(self):
        self.check_refused(source={"return_status": "rejected"})
        self.check_refused(source={"return_status": "completed"})

    def test_all_units_depleted_leave_exactly_zero_book_value(self):
        credit = replace(self.credit, lines=(
            replace(self.credit.lines[0], quantity=D("5")),))
        source = replace(self.source, requested_quantity=D("5"))
        proposal = propose_billed_vendor_credit(source=source, credit=credit)
        self.assertEqual(proposal.inventory_quantity_after, D("0"))
        self.assertEqual(proposal.inventory_value_after, D("0"))

    def test_invalid_document_type_and_multi_item_are_rejected(self):
        self.check_refused(doc={"kind": DocumentType.CUSTOMER_CREDIT})
        self.check_refused(doc={"lines": ()})

    def test_no_negative_source_financial_snapshot(self):
        self.check_refused(source={"previously_credited_bill_line_quantity": D("-1")})
        self.check_refused(source={"position_book_value": D("-1")})


if __name__ == "__main__":
    unittest.main()
