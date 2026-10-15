"""Fail-closed, offline preflight for a tenant-specific PostgreSQL restore.

This is NOT a backup scheduler or a restore executor. A trusted backup process
must create encrypted artifacts and sign their manifests out of band. A restore
operator must still verify keys, approvals, isolation, recovery objectives and
complete a real restore drill. Never give this primitive database credentials.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_NAME = re.compile(r"^[a-z][a-z0-9_]{2,62}$")
_KEY_ID = re.compile(r"^[A-Za-z0-9:._/-]{6,200}$")
_FIELDS = frozenset({
    "version", "backup_id", "tenant_id", "database", "created_at",
    "encrypted_archive_sha256", "archive_format", "encryption", "key_id",
})


class RestorePreflightError(ValueError):
    """Restore candidate is not authenticated for the requested tenant."""


@dataclass(frozen=True, slots=True)
class VerifiedRestoreCandidate:
    backup_id: UUID
    tenant_id: UUID
    database: str
    created_at: datetime
    encrypted_archive_sha256: str
    key_id: str


def _unique_fields(pairs):
    record = {}
    for key, value in pairs:
        if key in record:
            raise RestorePreflightError("Duplicate manifest JSON field")
        record[key] = value
    return record


def verify_restore_candidate(
    manifest_bytes: bytes, signature: bytes, *,
    trusted_manifest_public_key_pem: bytes,
    encrypted_artifact_path: Path,
    expected_tenant_id: UUID,
    expected_database: str,
    now: datetime | None = None,
    maximum_age: timedelta = timedelta(days=30),
) -> VerifiedRestoreCandidate:
    """Authorize only a matching signed manifest and intact encrypted archive.

    Inputs expected_tenant_id, expected_database and the public key MUST come
    from privileged server configuration, never a caller-supplied HTTP body.
    Passing preflight does NOT mean the archive is decryptable or restorable.
    """
    if not isinstance(manifest_bytes, bytes) or not 0 < len(manifest_bytes) <= 8192:
        raise RestorePreflightError("Invalid manifest size")
    if not isinstance(signature, bytes) or len(signature) != 64:
        raise RestorePreflightError("Invalid signature format")
    if not isinstance(trusted_manifest_public_key_pem, bytes):
        raise RestorePreflightError("Pinned public key required")
    try:
        key = serialization.load_pem_public_key(trusted_manifest_public_key_pem)
        if not isinstance(key, Ed25519PublicKey):
            raise RestorePreflightError("Only pinned Ed25519 signing keys accepted")
        key.verify(signature, manifest_bytes)
    except (ValueError, TypeError, InvalidSignature) as error:
        raise RestorePreflightError("Manifest signature invalid") from error
    try:
        payload = json.loads(manifest_bytes.decode("utf-8"), object_pairs_hook=_unique_fields)
        if not isinstance(payload, dict) or set(payload) != _FIELDS:
            raise RestorePreflightError("Manifest schema mismatch")
        if type(payload["version"]) is not int or payload["version"] != 1:
            raise RestorePreflightError("Unsupported manifest schema")
        backup_id = UUID(payload["backup_id"])
        tenant_id = UUID(payload["tenant_id"])
        database = payload["database"]
        checksum = payload["encrypted_archive_sha256"]
        key_id = payload["key_id"]
        created = datetime.fromisoformat(payload["created_at"].replace("Z", "+00:00"))
    except (ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError) as error:
        raise RestorePreflightError("Invalid signed manifest") from error
    if (not isinstance(expected_tenant_id, UUID) or
        not isinstance(database, str) or not _NAME.fullmatch(database) or
        not isinstance(expected_database, str) or
        tenant_id != expected_tenant_id or database != expected_database):
        raise RestorePreflightError("Tenant or target database mismatch")
    if (payload["archive_format"] != "postgresql-custom" or
        payload["encryption"] != "envelope-aes256-gcm" or
        not isinstance(checksum, str) or not _SHA256.fullmatch(checksum) or
        not isinstance(key_id, str) or not _KEY_ID.fullmatch(key_id)):
        raise RestorePreflightError("Unapproved backup format or metadata")
    moment = now if now is not None else datetime.now(timezone.utc)
    if (not isinstance(moment, datetime) or moment.tzinfo is None or
        not isinstance(created, datetime) or created.tzinfo is None or
        not isinstance(maximum_age, timedelta) or
        maximum_age <= timedelta(0)):
        raise RestorePreflightError("Invalid time or backup age policy")
    if created > moment + timedelta(minutes=1) or moment - created > maximum_age:
        raise RestorePreflightError("Backup is stale or from the future")
    path = Path(encrypted_artifact_path)
    try:
        # This primitive must not accept symlinks. Run in a trusted private
        # restore directory, never a web-upload directory or shared host path.
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise RestorePreflightError("Artifact is not a regular file")
            digest = hashlib.sha256()
            with os.fdopen(fd, "rb", closefd=False) as artifact:
                for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
                    digest.update(chunk)
        finally:
            os.close(fd)
    except (OSError, TypeError, ValueError) as error:
        raise RestorePreflightError("Backup artifact unreadable or unsafe") from error
    if digest.hexdigest() != checksum:
        raise RestorePreflightError("Encrypted backup artifact checksum mismatch")
    return VerifiedRestoreCandidate(
        backup_id=backup_id, tenant_id=tenant_id, database=database,
        created_at=created, encrypted_archive_sha256=checksum, key_id=key_id,
    )
