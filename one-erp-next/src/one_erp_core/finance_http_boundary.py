"""Server-side, private finance report endpoint building block.

Requires a CONNECTED trusted session authenticator and a scoped psycopg 3
connection. There is NO authenticator implementation, HTTP server, route,
deployment or customer login in this prototype. Never expose token values
in URLs, logs, HTML or GitHub.

An authenticator MUST independently verify session signature, issuer,
audience, expiry, revocation, and tenant memberships. This module cannot
mint or validate trust itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol
from uuid import UUID

from .accounting import AccountingError
from .dashboard_presentation import render_finance_dashboard_html
from .finance_dashboard import build_finance_dashboard
from .report_repository import load_finance_snapshot


@dataclass(frozen=True, slots=True)
class AuthenticatedFinanceIdentity:
    actor_id: UUID
    allowed_tenants: frozenset[UUID]


class FinanceIdentityVerifier(Protocol):
    """Supply implementation using server-verified identities, not user claims."""

    def verify_access_token(self, raw_token: str) -> AuthenticatedFinanceIdentity:
        ...


@dataclass(frozen=True, slots=True)
class PrivateFinanceResponse:
    status: int
    headers: dict[str, str]
    body: str


_PRIVATE_HEADERS = {
    "Content-Type":"text/html; charset=utf-8",
    "Cache-Control":"private, no-store, max-age=0",
    "Pragma":"no-cache",
    "X-Content-Type-Options":"nosniff",
    "X-Frame-Options":"DENY",
    "Referrer-Policy":"no-referrer",
    "Content-Security-Policy":(
        "default-src 'none'; base-uri 'none'; object-src 'none'; "
        "frame-ancestors 'none'; form-action 'none'; script-src 'none'; "
        "style-src 'unsafe-inline'"
    ),
}


def render_private_dashboard_response(
    connection,
    *,
    raw_access_token: str,
    identity_verifier: FinanceIdentityVerifier,
    requested_tenant: UUID,
    as_of: date,
    from_date: date,
    currency: str,
    locale: str = "ar",
    minor_units: int = 2,
) -> PrivateFinanceResponse:
    """Produce a cache-protected HTML response after layered authorization.

    Authentication/tenant selection comes only from the *server-side* verifier.
    A client can request a company but never assign actor IDs or grants.
    The database membership and RLS checks provide a second defense layer.
    Permission errors are deliberately generic to avoid account enumeration.
    """
    if not isinstance(raw_access_token, str) or not raw_access_token.strip():
        raise AccountingError("Authentication required")
    identity = identity_verifier.verify_access_token(raw_access_token)
    if (not isinstance(identity, AuthenticatedFinanceIdentity) or
            not isinstance(identity.actor_id, UUID) or
            not isinstance(requested_tenant, UUID) or
            not isinstance(identity.allowed_tenants, frozenset) or
            requested_tenant not in identity.allowed_tenants or
            not all(isinstance(x, UUID) for x in identity.allowed_tenants)):
        raise AccountingError("Not authorized to view this company")
    snapshot = load_finance_snapshot(
        connection, actor_id=identity.actor_id,
        tenant_id=requested_tenant, as_of=as_of,
    )
    if snapshot.tenant_id != requested_tenant or not snapshot.company_name.strip():
        raise AccountingError("Company report not available")
    dashboard = build_finance_dashboard(
        snapshot,from_date=from_date,currency=currency,
    )
    html = render_finance_dashboard_html(
        dashboard,company_name=snapshot.company_name,
        locale=locale,minor_units=minor_units,
    )
    return PrivateFinanceResponse(200,dict(_PRIVATE_HEADERS),html)
