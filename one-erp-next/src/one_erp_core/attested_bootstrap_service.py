"""Trusted-backend bridge for independently signed tenant stage evidence.

This component has NO infrastructure mutation, secret creation, routing
changes, real IdP integration, or customer provisioning endpoint.
The injected connection MUST belong to the verified, authorized control-plane
backend only. No browser or untrusted client may execute the underlying SQL
stage functions or submit its own identity/approval.
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from .bootstrap_attestation import (
    InvalidBootstrapEvidence, VerifiedBootstrapEvidence,
    verify_bootstrap_attestation,
)


class BootstrapCheckpointUnavailable(RuntimeError):
    pass


def record_signed_checkpoint(
    *,
    connection,
    claims_b64: str,
    signature_b64: str,
    pinned_public_key: bytes,
    pinned_issuer: str,
    trusted_job: UUID,
    trusted_tenant: UUID,
    trusted_operator: UUID,
    trusted_event: UUID,
    trusted_stage: str,
    checked_at: datetime,
) -> tuple[str, VerifiedBootstrapEvidence]:
    """Verify signature and then submit a single audited stage transition.

    Calls are transactional on the *control-plane* database, never the
    customer's ledger database. This helper cannot independently prove that
    a backup was restored or a TLS certificate has been checked: the trusted
    evidence-signing service must make those checks first.
    """
    verified = verify_bootstrap_attestation(
        claims_b64=claims_b64, signature_b64=signature_b64,
        trusted_public_key=pinned_public_key, expected_issuer=pinned_issuer,
        expected_job=trusted_job, expected_tenant=trusted_tenant,
        expected_operator=trusted_operator, expected_event=trusted_event,
        expected_stage=trusted_stage, at=checked_at,
    )
    try:
        with connection.transaction():
            row = connection.execute(
                "SELECT one_auth.advance_tenant_bootstrap(%s,%s,%s,%s,%s)",
                (
                    verified.job_id, verified.operator_id, verified.stage,
                    verified.evidence_digest, verified.event_id,
                ),
            ).fetchone()
            if row is None or row[0] not in (
                "requested", "database_created", "schema_migrated",
                "isolation_verified", "restore_verified",
                "route_verified", "ready",
            ):
                raise BootstrapCheckpointUnavailable(
                    "Unexpected control-plane transition status"
                )
        return row[0], verified
    except Exception:
        # Keep raw SQL/connection errors out of user-facing responses.
        raise BootstrapCheckpointUnavailable(
            "Signed bootstrap checkpoint could not be recorded"
        ) from None
