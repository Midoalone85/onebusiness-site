"""Offline authentication boundary tests; keys and subjects are CI-only."""
import unittest
from datetime import datetime, timezone
from uuid import uuid4

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from one_erp_core.accounting import AccountingError
from one_erp_core.finance_http_boundary import AuthenticatedFinanceIdentity
from one_erp_core.oidc_identity import RS256VerifierConfig, VerifiedRS256FinanceAccess


class Directory:
    def __init__(self, subject, identity):
        self.subject, self.identity = subject, identity
        self.calls = 0

    def resolve(self, issuer, subject):
        self.calls += 1
        if subject != self.subject:
            raise AccountingError("User is not registered")
        return self.identity


class RevocationRegistry:
    def __init__(self):
        self.blocked = set()
        self.calls = 0
        self.fail = False

    def is_revoked(self, issuer, jwt_id):
        self.calls += 1
        if self.fail:
            raise ConnectionError("Test revocation backend unavailable")
        return jwt_id in self.blocked


class OIDCSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private_key = rsa.generate_private_key(public_exponent=65537,key_size=2048)
        cls.private_pem = cls.private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        cls.public_pem = cls.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii")
        cls.attacker_pem = rsa.generate_private_key(public_exponent=65537,key_size=2048)

    def setUp(self):
        self.tenant, self.actor = uuid4(), uuid4()
        self.subject = "one-erp-test-subject"
        self.directory = Directory(
            self.subject,
            AuthenticatedFinanceIdentity(self.actor, frozenset({self.tenant})),
        )
        self.revocations = RevocationRegistry()
        self.config=RS256VerifierConfig(
            issuer="https://identity.example.test",
            audience="one-erp-finance-api",
            signing_keys_by_id={"key-1":self.public_pem},
        )
        self.verifier=VerifiedRS256FinanceAccess(
            self.config,self.directory,self.revocations,
        )
        self.now=int(datetime.now(timezone.utc).timestamp())

    def claims(self):
        return dict(
            iss=self.config.issuer,
            aud=self.config.audience,
            sub=self.subject,
            iat=self.now-10,
            nbf=self.now-10,
            exp=self.now+120,
            jti="unique-token-ci-1",
            token_use="access",
        )

    def sign(self, payload=None, *, key=None, kid="key-1", algorithm="RS256", extra_header=None):
        hdr={"kid":kid}
        hdr.update(extra_header or {})
        return jwt.encode(
            payload or self.claims(), key or self.private_pem,
            algorithm=algorithm, headers=hdr,
        )

    def test_valid_signed_access_token_returns_server_directory_grants(self):
        identity=self.verifier.verify_access_token(self.sign())
        self.assertEqual(identity.actor_id,self.actor)
        self.assertEqual(identity.allowed_tenants,frozenset({self.tenant}))
        self.assertEqual(self.directory.calls,1)
        self.assertEqual(self.revocations.calls,1)

    def test_untrusted_tenant_claim_cannot_grant_membership(self):
        attacker_tenant=uuid4()
        token=self.sign(dict(self.claims(),tenant_ids=[str(attacker_tenant)]))
        identity=self.verifier.verify_access_token(token)
        self.assertEqual(identity.allowed_tenants,frozenset({self.tenant}))
        self.assertNotIn(attacker_tenant,identity.allowed_tenants)

    def test_signature_unknown_key_and_alg_confusion_rejected(self):
        wrong_signature=self.sign(key=self.attacker_pem)
        unknown_kid=self.sign(kid="untrusted-key")
        hs_token=jwt.encode(self.claims(),"malicious-secret",
                            algorithm="HS256",headers={"kid":"key-1"})
        for token in (wrong_signature,unknown_kid,hs_token,"opaque-token", "", "x."*6000):
            with self.subTest(token_type=token[:20]):
                with self.assertRaises(AccountingError):
                    self.verifier.verify_access_token(token)
        self.assertEqual(self.directory.calls,0)

    def test_reject_jku_header_injected_key_url(self):
        token=self.sign(extra_header={"jku":"https://attacker.example.test/keys"})
        with self.assertRaises(AccountingError):
            self.verifier.verify_access_token(token)
        self.assertEqual(self.directory.calls,0)

    def test_issuer_and_audience_are_exact(self):
        for claim, value in (
            ("iss","https://evil.example.test"),
            ("aud","some-other-api"),
            ("aud",["one-erp-finance-api","other-api"]),
        ):
            payload=self.claims()
            payload[claim]=value
            with self.subTest(claim=claim,value=str(value)):
                with self.assertRaises(AccountingError):
                    self.verifier.verify_access_token(self.sign(payload))
        self.assertEqual(self.directory.calls,0)

    def test_expiration_nbf_iat_and_lifetime_guards(self):
        variations=[
            dict(exp=self.now-100),
            dict(nbf=self.now+120),
            dict(iat=self.now+120),
            dict(iat=self.now-5000),
            dict(exp=self.now+2000),
            dict(exp=self.now-11),
        ]
        for values in variations:
            payload=self.claims()
            payload.update(values)
            with self.subTest(values=values):
                with self.assertRaises(AccountingError):
                    self.verifier.verify_access_token(self.sign(payload))

    def test_required_claims_and_access_only(self):
        for key in ("iss","aud","sub","exp","iat","nbf","jti"):
            claims=self.claims()
            del claims[key]
            with self.subTest(missing=key):
                with self.assertRaises(AccountingError):
                    self.verifier.verify_access_token(self.sign(claims))
        wrong=self.claims()
        wrong["token_use"]="id"
        with self.assertRaises(AccountingError):
            self.verifier.verify_access_token(self.sign(wrong))

    def test_jti_revocation_and_fail_closed_registry(self):
        token=self.sign()
        self.revocations.blocked.add("unique-token-ci-1")
        with self.assertRaises(AccountingError):
            self.verifier.verify_access_token(token)
        self.revocations.blocked.clear()
        self.revocations.fail=True
        with self.assertRaises(AccountingError):
            self.verifier.verify_access_token(token)

    def test_subject_mapping_failure_and_bad_identity_rejected(self):
        token=self.sign()
        self.directory.subject="different-sub"
        with self.assertRaises(AccountingError):
            self.verifier.verify_access_token(token)
        self.directory.subject=self.subject
        self.directory.identity=AuthenticatedFinanceIdentity(
            self.actor, frozenset({"not-a-uuid"}),
        )
        with self.assertRaises(AccountingError):
            self.verifier.verify_access_token(token)

    def test_config_requires_https_issuer_and_pinned_public_keys(self):
        with self.assertRaises(ValueError):
            RS256VerifierConfig("http://untrusted.test","one-erp-finance-api",{"one":self.public_pem})
        with self.assertRaises(ValueError):
            RS256VerifierConfig("https://identity.example.test","one-erp-finance-api",{})


if __name__ == "__main__":
    unittest.main()
