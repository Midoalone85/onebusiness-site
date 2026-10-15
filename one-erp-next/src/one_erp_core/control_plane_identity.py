"""Read-only issuer subject mapping and JWT revocation from a control-plane DB.

The production database should be physically separate from all tenant
financial databases, and only a trusted backend may use the lookup connection.
This module NEVER provisions users, credentials, memberships or tokens.
"""
from __future__ import annotations

from contextlib import closing
from hashlib import sha256
from typing import Callable
from uuid import UUID

from .accounting import AccountingError
from .finance_http_boundary import AuthenticatedFinanceIdentity


class ControlPlaneIdentityDirectory:
    """Lookup *server-managed* actor and tenant access, not token claims."""

    def __init__(self, connection_factory: Callable):
        self._connect = connection_factory

    def resolve(self, issuer: str, subject: str) -> AuthenticatedFinanceIdentity:
        if not issuer.startswith("https://") or not subject or len(subject)>255:
            raise AccountingError("Invalid identity reference")
        with closing(self._connect()) as connection:
            with connection.transaction():
                with connection.cursor() as cur:
                    cur.execute(
                        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                    )
                    cur.execute(
                        """SELECT actor_id,active FROM one_auth.subject_links
                           WHERE issuer=%s AND subject=%s""",
                        (issuer,subject),
                    )
                    row=cur.fetchone()
                    if row is None or not row[1] or not isinstance(row[0],UUID):
                        raise AccountingError("Unknown or disabled identity")
                    actor=row[0]
                    cur.execute(
                        """SELECT tenant_id FROM one_auth.tenant_entitlements
                           WHERE actor_id=%s AND active ORDER BY tenant_id""",
                        (actor,),
                    )
                    tenants=frozenset(row[0] for row in cur.fetchall())
                    if not tenants or not all(isinstance(t,UUID) for t in tenants):
                        raise AccountingError("Identity has no authorized companies")
        return AuthenticatedFinanceIdentity(actor,tenants)


class ControlPlaneRevocationChecker:
    """Deny if a token's issuer-bound SHA-256 digest is actively revoked."""

    def __init__(self,connection_factory:Callable):
        self._connect=connection_factory

    def is_revoked(self,issuer:str,jwt_id:str)->bool:
        if (not issuer.startswith("https://") or not isinstance(jwt_id,str)
            or not 0<len(jwt_id)<=255):
            raise AccountingError("Invalid token reference")
        digest=sha256((issuer+"\0"+jwt_id).encode("utf-8")).digest()
        with closing(self._connect()) as connection:
            with connection.transaction():
                with connection.cursor() as cur:
                    cur.execute(
                        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                    )
                    cur.execute(
                        """SELECT 1 FROM one_auth.revoked_jti
                           WHERE token_digest=%s AND expires_at>now()""",
                        (digest,),
                    )
                    row=cur.fetchone()
        return row is not None
