"""ONE ERP NEXT 040: no-bank-connection evidence candidate tests."""
import unittest
from dataclasses import replace
from datetime import date
from decimal import Decimal
from uuid import uuid4

from one_erp_core.accounting import AccountingError
from one_erp_core.refund_bank_match_policy import (
    ApprovedRefundVoucher, ImportedBankDebitCandidate,
    check_refund_bank_candidate,
)


class RefundBankMatchPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tenant, self.order = uuid4(), uuid4()
        self.claim_maker, self.preparer, self.approver = uuid4(),uuid4(),uuid4()
        self.voucher = ApprovedRefundVoucher(
            tenant_id=self.tenant,order_id=self.order,
            customer_reference="CUSTOMER-REF",
            currency="SAR",amount=Decimal("10.00"),
            beneficiary_evidence_ref="BENEFICIARY-REFERENCE",
            approved_on=date(2026,10,15),
            claim_maker_id=self.claim_maker,
            order_maker_id=self.preparer,approver_id=self.approver,
            approval_state="approved",
        )
        self.line=ImportedBankDebitCandidate(
            tenant_id=self.tenant,bank_line_id=uuid4(),
            source_bank_account_ref="BANK-ACCOUNT-TEST",
            currency="SAR",debit_amount=Decimal("10.00"),
            value_date=date(2026,10,16),
            beneficiary_evidence_ref="BENEFICIARY-REFERENCE",
            payment_reference="BANK-REFUND-" + str(self.order),
            statement_import_reference="IMPORT-TEST-LEDGER-001",
        )

    def check(self, voucher=None, line=None):
        return check_refund_bank_candidate(voucher or self.voucher,line or self.line)

    def test_approval_and_identical_debit_returns_candidate_not_paid(self):
        result=self.check()
        self.assertEqual(result.amount,Decimal("10.00"))
        self.assertEqual(result.bank_line_id,self.line.bank_line_id)
        self.assertEqual(result.recommendation,"candidate_match_manual_review_required")

    def test_different_tenant_or_currencies_refused(self):
        for debit in (
            replace(self.line,tenant_id=uuid4()),
            replace(self.line,currency="USD"),
        ):
            with self.assertRaises(AccountingError):
                self.check(line=debit)

    def test_unapproved_or_same_person_approval_refused(self):
        for voucher in (
            replace(self.voucher,approval_state="requested"),
            replace(self.voucher,approver_id=self.claim_maker),
            replace(self.voucher,approver_id=self.preparer),
        ):
            with self.assertRaises(AccountingError):
                self.check(voucher=voucher)

    def test_absent_statement_reference_and_unmatched_order_refused(self):
        for change in (
            {"payment_reference":"BANK-PAYMENT-OTHER"},
            {"statement_import_reference":""},
            {"source_bank_account_ref":"x"},
            {"beneficiary_evidence_ref":"OTHER-BENEFICIARY"},
        ):
            with self.assertRaises(AccountingError):
                self.check(line=replace(self.line,**change))

    def test_payment_amount_wrong_sign_precision_nan_and_float_refused(self):
        for amount in (
            Decimal("-10.00"),Decimal("10.01"),Decimal("NaN"),10.00,
            Decimal("0")
        ):
            with self.assertRaises(AccountingError):
                self.check(line=replace(self.line,debit_amount=amount))
        with self.assertRaises(AccountingError):
            self.check(voucher=replace(self.voucher,amount=Decimal("10.001")))

    def test_bank_record_cannot_predate_approval(self):
        with self.assertRaises(AccountingError):
            self.check(line=replace(self.line,value_date=date(2026,10,14)))
