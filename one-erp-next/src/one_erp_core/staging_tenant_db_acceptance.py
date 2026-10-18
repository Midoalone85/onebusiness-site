"""Explicit, read-only 3-layer acceptance for a STAGING finance report reader.

All inputs must be pinned by a separately authorized operator; never accept
tenant IDs, SQL roles, connection factories, or grant policies from HTTP.
Requires two distinct, independently provisioned connections:
  * an elevated CONTROL-PLANE INSPECTOR to the expected dedicated tenant DB
  * a RESTRICTED FINANCE REPORT READER to the exact same tenant DB

No user or company provisioning, role changes, backups, route activation or
production approval occur. This is DB-only evidence, not Oracle readiness.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from uuid import UUID

from .accounting import AccountingError
from .tenant_inventory import (
    ExclusiveTenantInventory, verify_exclusive_tenant_inventory,
)
from .tenant_readiness import (
    DatabaseReadinessEvidence, verify_physical_tenant_database,
)
from .runtime_reader_privilege_audit import (
    RestrictedReaderPolicy, RestrictedReaderEvidence,
    inspect_restricted_reader,
)

_NAME = re.compile(r"[a-z][a-z0-9_]{5,62}\Z")


class StagingTenantDatabaseBlocked(RuntimeError):
    """Operator-visible generic failure, never a DSN or SQL details."""


@dataclass(frozen=True, slots=True)
class StagingTenantDBEvidence:
    """All three DB gates were evaluated; this is NOT release attestation."""
    tenant_id: UUID
    database_name: str
    reader_role: str
    verified_peer_roles: tuple[str, ...]
    catalog_relations_checked: int
    catalog_functions_checked: int
    approved_table_reads: tuple[str, ...]
    status: str = "STAGING_DB_PREFLIGHT_ONLY"
    route_ready: bool = False
    release_approved: bool = False


def verify_staging_finance_reader(
    *, privileged_inspector, restricted_reader,
    expected_tenant: UUID, expected_actor: UUID,
    expected_database: str, expected_reader_role: str,
    forbidden_peer_roles: tuple[str, ...],
    reviewed_reader_policy: RestrictedReaderPolicy,
) -> StagingTenantDBEvidence:
    """Run physical inventory, real reader membership/RLS, effective ACL audit.

    All three checks must succeed. A peer inventory is an operator guarantee;
    this cannot enumerate network pg_hba/firewall or guarantee the list is
    complete. The three checks read separate transactions and are therefore
    one time-bounded audit, not an atomic cross-connection certification.
    """
    if (not isinstance(expected_tenant, UUID) or expected_tenant.int == 0 or
        not isinstance(expected_actor, UUID) or expected_actor.int == 0 or
        not isinstance(expected_database, str) or
        not _NAME.fullmatch(expected_database) or
        not isinstance(expected_reader_role, str) or
        not _NAME.fullmatch(expected_reader_role) or
        not isinstance(reviewed_reader_policy, RestrictedReaderPolicy) or
        reviewed_reader_policy.login_role != expected_reader_role or
        not isinstance(forbidden_peer_roles, tuple) or
        not forbidden_peer_roles or
        any(not isinstance(role, str) or not _NAME.fullmatch(role) or
            role == expected_reader_role for role in forbidden_peer_roles) or
        len(set(forbidden_peer_roles)) != len(forbidden_peer_roles) or
        privileged_inspector is None or restricted_reader is None or
        privileged_inspector is restricted_reader):
        raise StagingTenantDatabaseBlocked("Pinned distinct staging SQL inspections required")

    try:
        # First: independent elevated scan sees hidden tenant rows and
        # checks exact DB/role and peer CONNECT denial.
        inventory = verify_exclusive_tenant_inventory(
            privileged_inspector, expected_tenant=expected_tenant,
            expected_database=expected_database,
            expected_reader_role=expected_reader_role,
            forbidden_peer_roles=forbidden_peer_roles,
        )
        if (not isinstance(inventory, ExclusiveTenantInventory) or
            inventory.tenant_id != expected_tenant or
            inventory.database_name != expected_database or
            inventory.reader_role != expected_reader_role or
            inventory.rejected_peer_roles != tuple(sorted(forbidden_peer_roles))):
            raise AccountingError("Inconsistent elevated tenant inventory")

        # Second: actual restricted SQL LOGIN must see only its own tenant,
        # hold an active verified membership and a reviewed forced-RLS schema.
        membership = verify_physical_tenant_database(
            restricted_reader, expected_tenant=expected_tenant,
            expected_actor=expected_actor, expected_database=expected_database,
            expected_reader_role=expected_reader_role,
        )
        if (not isinstance(membership, DatabaseReadinessEvidence) or
            membership.tenant_id != expected_tenant or
            membership.database_name != expected_database or
            membership.role_name != expected_reader_role):
            raise AccountingError("Restricted tenant reader check mismatch")

        # Third: independently inspect ALL effective read/write/EXECUTE
        # privileges including PUBLIC, column ACLs, custom schemas and roles.
        grants = inspect_restricted_reader(
            privileged_inspector, expected_database=expected_database,
            policy=reviewed_reader_policy,
        )
        if (not isinstance(grants, RestrictedReaderEvidence) or
            grants.database_name != expected_database or
            grants.login_role != expected_reader_role):
            raise AccountingError("Reader policy and DB catalog disagree")
    except Exception:
        raise StagingTenantDatabaseBlocked(
            "Staging tenant database security preflight blocked"
        ) from None

    return StagingTenantDBEvidence(
        tenant_id=expected_tenant, database_name=expected_database,
        reader_role=expected_reader_role,
        verified_peer_roles=inventory.rejected_peer_roles,
        catalog_relations_checked=grants.checked_tables,
        catalog_functions_checked=grants.checked_functions,
        approved_table_reads=grants.approved_table_reads,
    )
