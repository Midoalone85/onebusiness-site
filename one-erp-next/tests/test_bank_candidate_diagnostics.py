import unittest
from decimal import Decimal
from one_erp_core.bank_candidate_diagnostics import bank_candidate_mismatches

class CandidateDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.approved=("order","invoice","partner","SAR",Decimal("10"),
                       "BENEFICIARY-REVIEW-CI","INDEPENDENT-REVIEW-CI",
                       "approved_not_disbursed")
        self.candidate={"currency":"SAR","amount":Decimal("10"),
                        "beneficiary":"BENEFICIARY-REVIEW-CI",
                        "payment_reference":"EXTERNAL-REF"}

    def test_matching_fields(self):
        self.assertEqual(bank_candidate_mismatches(self.approved,self.candidate),[])

    def test_mismatches(self):
        self.assertEqual(bank_candidate_mismatches(self.approved,
            {**self.candidate,"currency":"USD","amount":11,"beneficiary":"X"}),
            ["currency","amount","beneficiary"])

    def test_missing_approval(self):
        self.assertEqual(bank_candidate_mismatches(None,self.candidate),
                         ["approved_instruction_missing"])

    def test_not_disbursed_proof(self):
        self.assertEqual(bank_candidate_mismatches(
            (*self.approved[:-1],"paid"),self.candidate),["not_approved_unpaid"])

if __name__ == "__main__":
    unittest.main()
