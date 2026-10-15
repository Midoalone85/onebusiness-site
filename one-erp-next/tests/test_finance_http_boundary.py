"""Trusted identity injection and private HTTP response boundary tests."""
import unittest
from dataclasses import replace
from datetime import date
from uuid import uuid4

from one_erp_core.accounting import AccountingError
from one_erp_core.finance_http_boundary import (
    AuthenticatedFinanceIdentity,render_private_dashboard_response,
)


class SampleVerifier:
    def __init__(self,identity):
        self.identity=identity
        self.called=0

    def verify_access_token(self,raw_token):
        self.called+=1
        if raw_token != "signed-session-demo":
            raise AccountingError("Authentication failed")
        return self.identity


class MustNotTouchDB:
    def transaction(self):
        raise AssertionError("Database accessed before identity authorization")


class FinanceHTTPBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tenant,self.actor=uuid4(),uuid4()
        self.identity=AuthenticatedFinanceIdentity(
            self.actor,frozenset({self.tenant}),
        )

    def call(self,verifier=None,**options):
        request=dict(
            raw_access_token="signed-session-demo",
            identity_verifier=verifier or SampleVerifier(self.identity),
            requested_tenant=self.tenant,
            as_of=date(2026,10,15),
            from_date=date(2026,10,1),
            currency="SAR",
        )
        request.update(options)
        return render_private_dashboard_response(MustNotTouchDB(),**request)

    def test_reject_unrelated_company_before_opening_database(self):
        with self.assertRaises(AccountingError):
            self.call(requested_tenant=uuid4())

    def test_reject_missing_token_before_verification(self):
        verifier=SampleVerifier(self.identity)
        with self.assertRaises(AccountingError):
            self.call(verifier=verifier,raw_access_token="  ")
        self.assertEqual(verifier.called,0)

    def test_reject_bad_token_before_database(self):
        with self.assertRaises(AccountingError):
            self.call(raw_access_token="invalid")

    def test_reject_fake_or_ill_typed_identity(self):
        bad=[
            AuthenticatedFinanceIdentity(self.actor,{self.tenant}),
            AuthenticatedFinanceIdentity("not-a-uuid",frozenset({self.tenant})),
            AuthenticatedFinanceIdentity(self.actor,frozenset({"not-a-uuid"})),
            None,
        ]
        for identity in bad:
            with self.subTest(identity=identity):
                with self.assertRaises(AccountingError):
                    self.call(verifier=SampleVerifier(identity))


if __name__=="__main__":
    unittest.main()
