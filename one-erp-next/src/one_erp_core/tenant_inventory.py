"""Independent elevated read-only inventory for a dedicated tenant database.

Unlike a tenant-scoped reporter, a *separately authorized* inspector can see
rows hidden by forced PostgreSQL RLS. This is an offline provisioning preflight,
not a route, login endpoint, credential manager or customer activation service.
An independent approval/MFA/secret-storage workflow must authorize the probe.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from json import dumps
import re
from uuid import UUID

from .accounting import AccountingError

_DB_ID = re.compile(r"^[a-z][a-z0-9_]{5,62}$")


@dataclass(frozen=True, slots=True)
class ExclusiveTenantInventory:
    tenant_id: UUID
    database_name: str
    reader_role: str
    rejected_peer_roles: tuple[str, ...]
    digest: bytes


def verify_exclusive_tenant_inventory(
    connection, *,
    expected_tenant: UUID,
    expected_database: str,
    expected_reader_role: str,
    forbidden_peer_roles: tuple[str, ...],
) -> ExclusiveTenantInventory:
    """Deny hidden extra tenants, PUBLIC CONNECT, and peer-reader CONNECT.

    This requires a *separate privileged inspector* connection that can bypass
    RLS and is supplied only by the trusted infrastructure control plane. The
    function performs no writes and enables a read-only transaction even for a
    privileged inspector. Caller must independently authenticate the operator,
    establish a pinned connection, enumerate all peer roles, and verify network
    ACLs/pg_hba; a positive result alone does not authorize tenant activation.
    """
    if (not isinstance(expected_tenant, UUID) or
            not isinstance(expected_database, str) or
            not isinstance(expected_reader_role, str) or
            not _DB_ID.fullmatch(expected_database) or
            not _DB_ID.fullmatch(expected_reader_role) or
            not isinstance(forbidden_peer_roles, tuple) or
            not forbidden_peer_roles or
            any(not isinstance(role, str) or not _DB_ID.fullmatch(role)
                or role == expected_reader_role for role in forbidden_peer_roles) or
            len(set(forbidden_peer_roles)) != len(forbidden_peer_roles)):
        raise AccountingError("Pinned database and independently enumerated peers required")

    try:
        with connection.transaction():
            with connection.cursor() as cur:
                cur.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                )
                cur.execute("SELECT current_database(), current_user")
                if cur.fetchone()[0] != expected_database:
                    raise AccountingError("Inspector bound to the wrong database")
                cur.execute(
                    "SELECT rolsuper,rolbypassrls FROM pg_roles "
                    "WHERE rolname=current_user"
                )
                inspector = cur.fetchone()
                if not inspector or not any(inspector):
                    raise AccountingError("Inspector cannot bypass hidden tenant rows")

                # LIMIT 2 detects a hidden second tenant without collecting PHI.
                cur.execute("SELECT id FROM public.one_tenants LIMIT 2")
                tenant_rows = cur.fetchall()
                if tenant_rows != [(expected_tenant,)]:
                    raise AccountingError("Database contains unexpected tenant identity")

                cur.execute(
                    "SELECT rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,"
                    "rolreplication,rolbypassrls FROM pg_roles WHERE rolname=%s",
                    (expected_reader_role,),
                )
                if cur.fetchone() != (True, False, False, False, False, False):
                    raise AccountingError("Dedicated reader has unsafe privileges")
                cur.execute(
                    "SELECT has_database_privilege(%s,%s,'CONNECT')",
                    (expected_reader_role, expected_database),
                )
                if cur.fetchone() != (True,):
                    raise AccountingError("Dedicated reader cannot connect")

                # PUBLIC CONNECT is silently granted on new databases unless
                # explicitly revoked. An RLS policy cannot compensate for it.
                cur.execute(
                    "SELECT EXISTS ("
                    " SELECT 1 FROM pg_database d,"
                    " LATERAL aclexplode(coalesce(d.datacl,"
                    " acldefault('d',d.datdba))) grant_item"
                    " WHERE d.datname=%s AND grant_item.grantee=0"
                    " AND grant_item.privilege_type='CONNECT')",
                    (expected_database,),
                )
                if cur.fetchone() != (False,):
                    raise AccountingError("Public database connection is enabled")
                for role in forbidden_peer_roles:
                    cur.execute(
                        "SELECT has_database_privilege(%s,%s,'CONNECT')",
                        (role, expected_database),
                    )
                    if cur.fetchone() != (False,):
                        raise AccountingError("Another company's reader can connect")
    except AccountingError:
        raise
    except Exception:
        # Do not leak catalog/connection details into caller responses.
        raise AccountingError("Exclusive tenant inventory unavailable") from None

    peers = tuple(sorted(forbidden_peer_roles))
    fingerprint = dumps({
        "kind": "one_exclusive_inventory_v1",
        "tenant_id": str(expected_tenant),
        "database": expected_database,
        "reader_role": expected_reader_role,
        "forbidden_peers": peers,
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return ExclusiveTenantInventory(
        tenant_id=expected_tenant, database_name=expected_database,
        reader_role=expected_reader_role, rejected_peer_roles=peers,
        digest=sha256(fingerprint).digest(),
    )
