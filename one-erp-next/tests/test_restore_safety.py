"""Restore safety regressions run entirely on disposable local artifacts."""
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from one_erp_core.restore_safety import (
    RestorePreflightError, verify_restore_candidate,
)


class RestoreSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tenant = uuid4()
        self.backup_id = uuid4()
        self.database = "one_customer_123"
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.signer = Ed25519PrivateKey.generate()
        self.pinned_public_key = self.signer.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.artifact = Path(self.temp.name) / "encrypted.pgdump"
        self.artifact.write_bytes(b"CI ONLY ENCRYPTED BACKUP PLACEHOLDER")
        self.checksum = hashlib.sha256(self.artifact.read_bytes()).hexdigest()

    def payload(self):
        return {
            "version": 1, "backup_id": str(self.backup_id),
            "tenant_id": str(self.tenant), "database": self.database,
            "created_at": self.now.isoformat().replace("+00:00", "Z"),
            "encrypted_archive_sha256": self.checksum,
            "archive_format": "postgresql-custom",
            "encryption": "envelope-aes256-gcm",
            "key_id": "kms:test-backup-1",
        }

    def check(self, payload=None, **kwargs):
        raw = json.dumps(
            self.payload() if payload is None else payload,
            sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return self.check_raw(raw, **kwargs)

    def check_raw(self, raw, **kwargs):
        signature = kwargs.pop("signature", self.signer.sign(raw))
        options = dict(
            trusted_manifest_public_key_pem=self.pinned_public_key,
            encrypted_artifact_path=self.artifact,
            expected_tenant_id=self.tenant,
            expected_database=self.database,
            now=self.now,
        )
        options.update(kwargs)
        return verify_restore_candidate(raw, signature, **options)

    def test_valid_signed_manifest_and_exact_encrypted_artifact(self):
        verified = self.check()
        self.assertEqual(verified.tenant_id, self.tenant)
        self.assertEqual(verified.backup_id, self.backup_id)
        self.assertEqual(verified.database, self.database)
        self.assertEqual(verified.encrypted_archive_sha256, self.checksum)

    def test_wrong_tenant_or_database_is_refused_before_restore(self):
        for options in (
            {"expected_tenant_id": uuid4()},
            {"expected_database": "one_customer_other"},
        ):
            with self.subTest(options=options), self.assertRaises(RestorePreflightError):
                self.check(**options)

    def test_tampered_manifest_and_untrusted_signer_fail_closed(self):
        raw = json.dumps(self.payload()).encode("utf-8")
        signature = self.signer.sign(raw)
        with self.assertRaises(RestorePreflightError):
            self.check_raw(raw + b" ", signature=signature)
        attacker = Ed25519PrivateKey.generate()
        with self.assertRaises(RestorePreflightError):
            self.check_raw(raw, signature=attacker.sign(raw))

    def test_changed_archive_fails_checksum(self):
        self.artifact.write_bytes(b"CORRUPTED BACKUP")
        with self.assertRaises(RestorePreflightError):
            self.check()

    def test_stale_and_future_manifest_is_rejected(self):
        for delta in (timedelta(days=-31), timedelta(hours=1)):
            payload = self.payload()
            payload["created_at"] = (
                self.now + delta
            ).isoformat().replace("+00:00", "Z")
            with self.subTest(delta=delta), self.assertRaises(RestorePreflightError):
                self.check(payload)

    def test_wrong_encryption_format_and_unexpected_fields_are_rejected(self):
        changes = (
            {"encryption": "none"}, {"archive_format": "plain-sql"},
            {"version": 2}, {"key_id": "bad whitespace"},
            {"encrypted_archive_sha256": "0"*64},
            {"unknown_field": "malicious"},
        )
        for changed in changes:
            payload = dict(self.payload(), **changed)
            with self.subTest(changed=changed), self.assertRaises(RestorePreflightError):
                self.check(payload)

    def test_signed_duplicate_fields_still_fail(self):
        raw = json.dumps(self.payload()).encode("utf-8")
        # Duplicate the same top-level property using an otherwise valid signature.
        raw = raw[:-1] + b',"version":1}'
        with self.assertRaises(RestorePreflightError):
            self.check_raw(raw)

    def test_symlink_archive_is_not_accepted(self):
        link = Path(self.temp.name) / "backup-link"
        link.symlink_to(self.artifact)
        with self.assertRaises(RestorePreflightError):
            self.check(encrypted_artifact_path=link)

    def test_signed_invalid_timestamp_fails_closed(self):
        payload = self.payload()
        payload["created_at"] = 1234
        with self.assertRaises(RestorePreflightError):
            self.check(payload)


if __name__ == "__main__":
    unittest.main()
