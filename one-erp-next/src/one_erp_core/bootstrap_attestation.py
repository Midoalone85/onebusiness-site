"""Verify signed, short-lived operator-independent tenant bootstrap evidence.

This module has NO privileged database operations, secret keys, network
access, provisioning driver, or automatic tenant activation.

A trusted backend MAY use its output before requesting an audited stage
transition. Approval, external evidence verification and operator permissions
remain separate requirements; a valid signature alone is NOT proof that a
backup restore, TLS check, or physical isolation actually occurred.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import base64
import json
import re
from uuid import UUID

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
except ImportError:
    Ed25519PublicKey = None
    InvalidSignature = None


_STAGES = frozenset({
    "database_created", "schema_migrated", "isolation_verified",
    "restore_verified", "route_verified", "ready",
})
_CLAIMS = frozenset({
    "type", "issuer", "job_id", "tenant_id", "operator_id", "event_id",
    "stage", "evidence_sha256", "issued_at", "expires_at",
})
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_ISSUER = re.compile(r"^[a-z][a-z0-9._:-]{4,80}$")
_MAX_PAYLOAD = 1400
_MAX_TTL = timedelta(minutes=10)
_CLOCK_SKEW = timedelta(seconds=30)


class InvalidBootstrapEvidence(ValueError):
    """Signed checkpoint rejected without exposing internal account details."""


@dataclass(frozen=True, slots=True)
class VerifiedBootstrapEvidence:
    job_id: UUID
    tenant_id: UUID
    operator_id: UUID
    event_id: UUID
    stage: str
    evidence_digest: bytes
    signed_claims_digest: bytes


def _strict_pairs(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise InvalidBootstrapEvidence("Duplicate checkpoint field")
        obj[key] = value
    return obj


def _decode_unpadded(encoded: str) -> bytes:
    if (not isinstance(encoded, str) or not encoded or
            not re.fullmatch(r"[A-Za-z0-9_-]+", encoded)):
        raise InvalidBootstrapEvidence("Invalid attestation encoding")
    raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    if base64.urlsafe_b64encode(raw).rstrip(b"=").decode() != encoded:
        raise InvalidBootstrapEvidence("Noncanonical attestation encoding")
    return raw


def verify_bootstrap_attestation(
    *,
    claims_b64: str,
    signature_b64: str,
    trusted_public_key: bytes,
    expected_issuer: str,
    expected_job: UUID,
    expected_tenant: UUID,
    expected_operator: UUID,
    expected_event: UUID,
    expected_stage: str,
    at: datetime,
) -> VerifiedBootstrapEvidence:
    """Validate a pinned independent Ed25519 checkpoint attestation.

    Inputs such as issuer/job/tenant/actor/event/stage MUST be supplied from
    an approved trusted server workflow, never copied from attestation JSON
    or from browser-supplied parameters. The verification key must be pinned
    through a separately secured key lifecycle.
    """
    if Ed25519PublicKey is None:
        raise InvalidBootstrapEvidence("Attestation verification unavailable")
    if (not isinstance(expected_issuer, str) or
            not _ISSUER.fullmatch(expected_issuer) or
            not isinstance(trusted_public_key, bytes) or
            len(trusted_public_key) != 32 or
            not all(isinstance(x, UUID) for x in (
                expected_job, expected_tenant, expected_operator,
                expected_event,
            )) or expected_stage not in _STAGES or
            not isinstance(at, datetime) or at.tzinfo is None or
            at.utcoffset() is None):
        raise InvalidBootstrapEvidence("Invalid trusted attestation configuration")
    payload = _decode_unpadded(claims_b64)
    signature = _decode_unpadded(signature_b64)
    if len(payload) > _MAX_PAYLOAD or len(signature) != 64:
        raise InvalidBootstrapEvidence("Invalid attestation size")
    try:
        public_key = Ed25519PublicKey.from_public_bytes(trusted_public_key)
        public_key.verify(signature, payload)
    except (ValueError, TypeError, InvalidSignature):
        raise InvalidBootstrapEvidence("Checkpoint signature invalid") from None
    try:
        claims = json.loads(payload.decode("utf-8"), object_pairs_hook=_strict_pairs)
    except (UnicodeError, ValueError, TypeError):
        raise InvalidBootstrapEvidence("Invalid checkpoint document") from None
    if (not isinstance(claims, dict) or set(claims) != _CLAIMS or
            any(not isinstance(value, str) for value in claims.values())):
        raise InvalidBootstrapEvidence("Unexpected checkpoint claims")
    encoded = json.dumps(
        claims, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    if encoded != payload:
        raise InvalidBootstrapEvidence("Noncanonical checkpoint document")
    if claims["type"] != "ONE_BOOTSTRAP_STAGE_V1":
        raise InvalidBootstrapEvidence("Unsupported checkpoint format")
    if claims["issuer"] != expected_issuer or claims["stage"] != expected_stage:
        raise InvalidBootstrapEvidence("Wrong checkpoint authority or stage")
    for field_name, expected in (
        ("job_id", expected_job), ("tenant_id", expected_tenant),
        ("operator_id", expected_operator), ("event_id", expected_event)
    ):
        if claims[field_name] != str(expected):
            raise InvalidBootstrapEvidence("Checkpoint identity mismatch")
    if not _HEX64.fullmatch(claims["evidence_sha256"]):
        raise InvalidBootstrapEvidence("Invalid evidence hash")
    if claims["evidence_sha256"] == "0" * 64:
        raise InvalidBootstrapEvidence("Empty evidence hash")
    try:
        issued = datetime.fromisoformat(claims["issued_at"])
        expires = datetime.fromisoformat(claims["expires_at"])
    except ValueError:
        raise InvalidBootstrapEvidence("Invalid checkpoint lifetime") from None
    if (issued.tzinfo is None or expires.tzinfo is None or
            issued.utcoffset() is None or expires.utcoffset() is None or
            issued.utcoffset() != timedelta(0) or
            expires.utcoffset() != timedelta(0) or
            issued >= expires or expires - issued > _MAX_TTL or
            issued > at + _CLOCK_SKEW or expires <= at):
        raise InvalidBootstrapEvidence("Checkpoint expired or outside freshness window")
    return VerifiedBootstrapEvidence(
        job_id=expected_job, tenant_id=expected_tenant,
        operator_id=expected_operator, event_id=expected_event,
        stage=expected_stage,
        evidence_digest=bytes.fromhex(claims["evidence_sha256"]),
        signed_claims_digest=sha256(payload).digest(),
    )
