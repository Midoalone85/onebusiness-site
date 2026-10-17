"""Regression tests for trusted JWKS refresh and secured report identity."""
import base64
import json
import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from one_erp_core.accounting import AccountingError
from one_erp_core.finance_http_boundary import AuthenticatedFinanceIdentity
from one_erp_core.trusted_jwks import (
    JWKSBackedFinanceAccess, TrustedJWKSStore, parse_rs256_jwks,
)


def b64uint(number):
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class TrustedSigningKeysTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.other_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def setUp(self):
        self.url = "https://identity.example.test/keys"
        self.store = TrustedJWKSStore(jwks_url=self.url)
        self.tenant = uuid4()
        self.actor = uuid4()

    def jwk(self, *, key=None, kid="rsa-1"):
        numbers = (key or self.private).public_key().public_numbers()
        return {"kid": kid, "kty": "RSA", "alg": "RS256", "use": "sig",
                "n": b64uint(numbers.n), "e": b64uint(numbers.e)}

    def document(self, *keys):
        return json.dumps({"keys": list(keys)}).encode("utf-8")

    def test_valid_jwks_authenticates_signed_token_with_local_membership(self):
        self.store.refresh_from_document(
            self.document(self.jwk()), source_url=self.url
        )

        class Directory:
            def resolve(_, issuer, subject):
                if (issuer, subject) != ("https://identity.example.test", "user-1"):
                    raise AccountingError("No membership")
                return AuthenticatedFinanceIdentity(
                    self.actor, frozenset({self.tenant})
                )

        class Revocations:
            def is_revoked(_, issuer, jti):
                return False

        verifier = JWKSBackedFinanceAccess(
            issuer="https://identity.example.test", audience="one-erp-finance-api",
            keys=self.store, directory=Directory(), revocations=Revocations()
        )
        now = int(datetime.now(timezone.utc).timestamp())
        claims = {
            "iss": "https://identity.example.test", "aud": "one-erp-finance-api",
            "sub": "user-1", "iat": now - 3, "nbf": now - 3,
            "exp": now + 120, "jti": "token-1", "token_use": "access",
            "tenant_ids": [str(uuid4())],
        }
        token = jwt.encode(claims, self.private, algorithm="RS256",
                           headers={"kid": "rsa-1"})
        identity = verifier.verify_access_token(token)
        self.assertEqual(identity.actor_id, self.actor)
        self.assertEqual(identity.allowed_tenants, frozenset({self.tenant}))

        forged = jwt.encode(claims, self.other_private, algorithm="RS256",
                            headers={"kid": "rsa-1"})
        with self.assertRaises(AccountingError):
            verifier.verify_access_token(forged)

        self.store._expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        with self.assertRaises(AccountingError):
            verifier.verify_access_token(token)

    def test_uninitialized_or_expired_store_denies_access(self):
        with self.assertRaises(AccountingError):
            self.store.valid_keys()
        self.store.refresh_from_document(self.document(self.jwk()), source_url=self.url)
        with self.assertRaises(AccountingError):
            self.store.valid_keys(now=datetime.now(timezone.utc) + timedelta(hours=1))

    def test_key_rotation_replaces_keys_and_does_not_keep_old_ids(self):
        self.store.refresh_from_document(self.document(self.jwk()), source_url=self.url)
        self.assertIn("rsa-1", self.store.valid_keys())
        self.store.refresh_from_document(
            self.document(self.jwk(key=self.other_private, kid="rsa-2")),
            source_url=self.url
        )
        self.assertEqual(set(self.store.valid_keys()), {"rsa-2"})

    def test_failed_refresh_never_extends_existing_key_lifetime(self):
        self.store.refresh_from_document(self.document(self.jwk()), source_url=self.url)
        expiry = self.store._expires_at
        with self.assertRaises(ValueError):
            self.store.refresh_from_document(
                self.document(self.jwk()), source_url="https://other.test/keys"
            )
        with self.assertRaises(ValueError):
            self.store.refresh_from_document(
                self.document(dict(self.jwk(), d="private-forbidden")),
                source_url=self.url
            )
        self.assertEqual(self.store._expires_at, expiry)

    def test_reject_duplicate_ids_and_duplicate_json_members(self):
        with self.assertRaises(ValueError):
            parse_rs256_jwks(self.document(self.jwk(), self.jwk()))
        with self.assertRaises(ValueError):
            parse_rs256_jwks('{"keys":[],"keys":[]}')
        with self.assertRaises(ValueError):
            parse_rs256_jwks('{"keys":[{"kid":"a","kid":"b"}]}')

    def test_reject_private_material_or_remote_pointer(self):
        for invalid in (
            dict(self.jwk(), d="secret"),
            dict(self.jwk(), x5u="https://attacker.test/key"),
            dict(self.jwk(), jku="https://attacker.test/key"),
            dict(self.jwk(), key_ops=["sign"]),
            dict(self.jwk(), kid="../key"),
        ):
            with self.subTest(fields=list(invalid)):
                with self.assertRaises(ValueError):
                    parse_rs256_jwks(self.document(invalid))

    def test_reject_invalid_modulus_exponent_encoding_and_empty_keys(self):
        for invalid in (
            dict(self.jwk(), n="AQAB"),
            dict(self.jwk(), e="Aw"),
            dict(self.jwk(), n="!!"),
            dict(self.jwk(), n="A" + self.jwk()["n"]),
            dict(self.jwk(), n=self.jwk()["n"] + "="),
        ):
            with self.subTest(n=invalid.get("n"), e=invalid.get("e")):
                with self.assertRaises(ValueError):
                    parse_rs256_jwks(self.document(invalid))
        with self.assertRaises(ValueError):
            parse_rs256_jwks(self.document({"kty": "EC", "kid": "ec"}))

    def test_reject_unbounded_documents(self):
        with self.assertRaises(ValueError):
            parse_rs256_jwks(b" " * 65537)
        with self.assertRaises(ValueError):
            parse_rs256_jwks(self.document(*([self.jwk(kid=str(i)) for i in range(17)])))

    def test_url_and_timestamp_require_hard_pinning(self):
        for url in (
            "http://identity.test/keys", "https://user:pw@identity.test/keys",
            "https://identity.test:8080/keys", "https://identity.test/keys?token=x",
            "https://identity.test/keys#fragment",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                TrustedJWKSStore(jwks_url=url)
        with self.assertRaises(ValueError):
            TrustedJWKSStore(jwks_url=self.url, max_age_seconds=99999)
        with self.assertRaises(ValueError):
            self.store.refresh_from_document(
                self.document(self.jwk()), source_url=self.url,
                fetched_at=datetime.now(timezone.utc) + timedelta(hours=1)
            )
        with self.assertRaises(ValueError):
            self.store.refresh_from_document(
                self.document(self.jwk()), source_url=self.url,
                fetched_at=datetime.now().replace(tzinfo=None)
            )


if __name__ == "__main__":
    unittest.main()
