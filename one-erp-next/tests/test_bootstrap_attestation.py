"""Negative security tests for ONE ERP Next offline signed tenant evidence.

Keys are generated in memory and thrown away. They are NOT deployment keys.
"""
import base64
import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from one_erp_core.bootstrap_attestation import (
    InvalidBootstrapEvidence, verify_bootstrap_attestation,
)


def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class TenantBootstrapAttestationTests(unittest.TestCase):
    def setUp(self):
        self.key = Ed25519PrivateKey.generate()
        self.public = self.key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        self.job, self.tenant, self.operator, self.event = (
            uuid4(), uuid4(), uuid4(), uuid4()
        )
        self.now = datetime(2026, 10, 15, 2, 0, tzinfo=timezone.utc)
        self.claims = {
            "type": "ONE_BOOTSTRAP_STAGE_V1",
            "issuer": "one-ci-evidence",
            "job_id": str(self.job),
            "tenant_id": str(self.tenant),
            "operator_id": str(self.operator),
            "event_id": str(self.event),
            "stage": "database_created",
            "evidence_sha256": sha256(b"isolated fictional CI evidence").hexdigest(),
            "issued_at": (self.now-timedelta(minutes=1)).isoformat(),
            "expires_at": (self.now+timedelta(minutes=2)).isoformat(),
        }

    def args(self, *, claims=None, key=None, raw=None):
        raw = raw or json.dumps(
            claims if claims is not None else self.claims,
            sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        secret = key or self.key
        return dict(
            claims_b64=b64(raw), signature_b64=b64(secret.sign(raw)),
            trusted_public_key=self.public, expected_issuer="one-ci-evidence",
            expected_job=self.job, expected_tenant=self.tenant,
            expected_operator=self.operator, expected_event=self.event,
            expected_stage="database_created", at=self.now,
        )

    def test_valid_checkpoint_signed_for_exact_ids_and_short_expiry(self):
        parsed = verify_bootstrap_attestation(**self.args())
        self.assertEqual(parsed.job_id, self.job)
        self.assertEqual(parsed.tenant_id, self.tenant)
        self.assertEqual(parsed.stage, "database_created")
        self.assertEqual(parsed.evidence_digest,
                         bytes.fromhex(self.claims["evidence_sha256"]))
        self.assertEqual(len(parsed.signed_claims_digest), 32)
        self.assertNotEqual(parsed.signed_claims_digest, parsed.evidence_digest)

    def test_other_key_and_tampering_fail_closed(self):
        signed = self.args()
        signed["claims_b64"] = b64(b"{}")
        with self.assertRaises(InvalidBootstrapEvidence):
            verify_bootstrap_attestation(**signed)
        with self.assertRaises(InvalidBootstrapEvidence):
            verify_bootstrap_attestation(
                **self.args(key=Ed25519PrivateKey.generate())
            )
        signed = self.args()
        signed["signature_b64"] = signed["signature_b64"][:-1] + "?"
        with self.assertRaises(InvalidBootstrapEvidence):
            verify_bootstrap_attestation(**signed)

    def test_strict_job_tenant_operator_event_issuer_and_stage_binding(self):
        config = self.args()
        for key in ("expected_job", "expected_tenant",
                    "expected_operator", "expected_event"):
            tampered = dict(config)
            tampered[key] = uuid4()
            with self.subTest(key=key), self.assertRaises(InvalidBootstrapEvidence):
                verify_bootstrap_attestation(**tampered)
        for field, value in (("expected_issuer", "another-issuer"),
                             ("expected_stage", "schema_migrated")):
            tampered = dict(config)
            tampered[field] = value
            with self.subTest(field=field), self.assertRaises(InvalidBootstrapEvidence):
                verify_bootstrap_attestation(**tampered)

    def test_any_claim_change_signed_by_same_key_is_still_checked(self):
        for field, value in [
            ("stage", "ready"),
            ("job_id", str(uuid4())),
            ("issuer", "other-authority"),
            ("evidence_sha256", "0" * 64),
            ("evidence_sha256", "not-a-digest"),
            ("type", "WRONG"),
            ("issued_at", "2026-10-15T14:00:00+00:00"),
            ("expires_at", "2026-10-14T14:00:00+00:00"),
        ]:
            modified = dict(self.claims, **{field: value})
            with self.subTest(field=field, value=value), self.assertRaises(
                InvalidBootstrapEvidence
            ):
                verify_bootstrap_attestation(**self.args(claims=modified))

    def test_short_lifetime_and_clock_skew(self):
        for issue_delta, exp_delta in [
            (0, 0),
            (timedelta(minutes=2), timedelta(minutes=4)),
            (-timedelta(minutes=2), timedelta(minutes=11)),
        ]:
            modified = dict(self.claims)
            modified["issued_at"] = (self.now+issue_delta).isoformat()
            modified["expires_at"] = (self.now+exp_delta).isoformat()
            with self.subTest(issue_delta=issue_delta), self.assertRaises(
                InvalidBootstrapEvidence
            ):
                verify_bootstrap_attestation(**self.args(claims=modified))
        signed = self.args()
        signed["at"] = datetime(2026, 10, 16, 2, tzinfo=timezone.utc)
        with self.assertRaises(InvalidBootstrapEvidence):
            verify_bootstrap_attestation(**signed)

    def test_reject_extra_missing_duplicate_or_noncanonical_json(self):
        for document in [
            dict(self.claims, debug="true"),
            {k: v for k, v in self.claims.items() if k != "operator_id"},
            dict(self.claims, tenant_id=123),
        ]:
            with self.assertRaises(InvalidBootstrapEvidence):
                verify_bootstrap_attestation(**self.args(claims=document))
        raw = json.dumps(self.claims, sort_keys=False).encode()
        with self.assertRaises(InvalidBootstrapEvidence):
            verify_bootstrap_attestation(**self.args(raw=raw))
        base = json.dumps(self.claims, sort_keys=True, separators=(",", ":"))
        raw = base.replace(
            '"stage":"database_created"',
            '"stage":"database_created","stage":"ready"',
        ).encode()
        with self.assertRaises(InvalidBootstrapEvidence):
            verify_bootstrap_attestation(**self.args(raw=raw))

    def test_invalid_public_key_and_untrusted_time_config_rejected(self):
        signed = self.args()
        signed["trusted_public_key"] = b"incorrect"
        with self.assertRaises(InvalidBootstrapEvidence):
            verify_bootstrap_attestation(**signed)
        signed = self.args()
        signed["at"] = datetime(2026, 10, 15, 2, 0)
        with self.assertRaises(InvalidBootstrapEvidence):
            verify_bootstrap_attestation(**signed)
        signed = self.args()
        signed["expected_issuer"] = "https://arbitrary.example/"
        with self.assertRaises(InvalidBootstrapEvidence):
            verify_bootstrap_attestation(**signed)


if __name__ == "__main__":
    unittest.main()
