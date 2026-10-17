"""Minimal authenticated WSGI route for private ONE ERP finance dashboards.

This is a *deployable building block*, not an independently deployed server.
Use a reviewed WSGI worker behind an HTTPS reverse proxy, with backend-only
connections, a real trusted identity verifier, secret-managed connections and
a per-tenant connection registry. Never expose psycopg or WSGI environ to
untrusted source code. The TLS terminator MUST set wsgi.url_scheme via trusted
server configuration, never via client-controlled X-Forwarded-Proto.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import date
from typing import Callable, Mapping
from urllib.parse import parse_qs
from uuid import UUID
import re

from .finance_http_boundary import (
    AuthenticatedFinanceIdentity, FinanceIdentityVerifier,
    PrivateFinanceResponse, render_private_dashboard_response,
)

_PATH = "/api/v1/finance/dashboard"
_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_TOKEN = re.compile(r"^Bearer ([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)$")
_COMMON_HEADERS = (
    ("Cache-Control", "private, no-store, max-age=0"),
    ("Pragma", "no-cache"),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Content-Security-Policy",
     "default-src 'none'; base-uri 'none'; object-src 'none'; "
     "frame-ancestors 'none'; script-src 'none'; form-action 'none'"),
    ("Vary", "Authorization"),
)


@dataclass(frozen=True, slots=True)
class TenantFinanceConnections:
    """A trusted server-owned registry, without wildcard/default fallback."""

    factories: Mapping[UUID, Callable[[], object]]

    def __post_init__(self):
        if not self.factories or any(
            not isinstance(tenant, UUID) or not callable(factory)
            for tenant, factory in self.factories.items()
        ):
            raise ValueError("Explicit tenant-to-connection mappings required")

    def open(self, tenant_id: UUID):
        factory = self.factories.get(tenant_id)
        if factory is None:
            raise PermissionError("Tenant database is not provisioned")
        return factory()


class _VerifiedIdentity:
    """Private adapter: avoid a second issuer lookup after verification."""

    def __init__(self, identity: AuthenticatedFinanceIdentity):
        self._identity = identity

    def verify_access_token(self, raw_token: str) -> AuthenticatedFinanceIdentity:
        return self._identity


class OneFinanceWSGI:
    """Single read-only HTML route. All financial errors fail closed.

    Authorization is checked BEFORE resolving a tenant DB factory. Unknown
    tenants, invalid JWTs and disabled companies never reach the ledger DB.
    """

    def __init__(
        self,
        *,
        verifier: FinanceIdentityVerifier,
        connections: TenantFinanceConnections,
        allowed_hosts: frozenset[str],
    ):
        if (not isinstance(allowed_hosts, frozenset) or not allowed_hosts or
            any(not isinstance(h, str) or not h or
                "/" in h or h != h.lower() or h.startswith(".")
                for h in allowed_hosts)):
            raise ValueError("Explicit lowercase HTTPS host allowlist required")
        self._verifier = verifier
        self._connections = connections
        self._allowed_hosts = allowed_hosts

    @staticmethod
    def _reply(start_response, status: str, body: str,
               *, headers: tuple[tuple[str, str], ...] = ()):
        data = body.encode("utf-8")
        start_response(status, list(_COMMON_HEADERS) + [
            ("Content-Type", "text/plain; charset=utf-8"),
            ("Content-Length", str(len(data))),
        ] + list(headers))
        return [data]

    @staticmethod
    def _date_arg(raw: str) -> date:
        if not _DATE.fullmatch(raw):
            raise ValueError("Invalid date")
        return date.fromisoformat(raw)

    def __call__(self, environ, start_response):
        # This check relies on the trusted WSGI server's TLS configuration.
        if environ.get("wsgi.url_scheme") != "https":
            return self._reply(start_response, "403 Forbidden", "HTTPS required")
        if environ.get("HTTP_HOST") not in self._allowed_hosts:
            return self._reply(start_response, "403 Forbidden", "Forbidden")
        if environ.get("REQUEST_METHOD") != "GET":
            return self._reply(start_response, "405 Method Not Allowed",
                               "Method not allowed", headers=(("Allow","GET"),))
        if environ.get("PATH_INFO") != _PATH:
            return self._reply(start_response, "404 Not Found", "Not found")
        # Disallow bodies on GET. Tokens and any credentials in URL are
        # rejected because only these exact query keys are accepted.
        if environ.get("CONTENT_LENGTH", "") not in ("", "0"):
            return self._reply(start_response, "400 Bad Request", "Invalid request")
        query = environ.get("QUERY_STRING", "")
        if not isinstance(query, str) or len(query) > 400:
            return self._reply(start_response, "400 Bad Request", "Invalid request")
        try:
            params = parse_qs(query, keep_blank_values=True, strict_parsing=True,
                              max_num_fields=5)
            if set(params) not in (
                {"tenant", "from", "as_of", "currency"},
                {"tenant", "from", "as_of", "currency", "locale"},
            ) or any(len(v) != 1 for v in params.values()):
                raise ValueError("Unexpected request fields")
            tenant = UUID(params["tenant"][0])
            from_date = self._date_arg(params["from"][0])
            as_of = self._date_arg(params["as_of"][0])
            currency = params["currency"][0]
            locale = params.get("locale", ["ar"])[0]
            if not _CURRENCY.fullmatch(currency) or locale not in ("ar","en"):
                raise ValueError("Unsupported locale or currency")
            if from_date > as_of or (as_of - from_date).days > 366:
                raise ValueError("Invalid reporting range")
        except (ValueError, KeyError, OverflowError):
            return self._reply(start_response, "400 Bad Request", "Invalid request")

        auth_header = environ.get("HTTP_AUTHORIZATION", "")
        if not isinstance(auth_header, str) or len(auth_header) > 8200:
            return self._reply(start_response, "401 Unauthorized",
                               "Authentication required")
        token_match = _TOKEN.fullmatch(auth_header)
        if token_match is None:
            return self._reply(start_response, "401 Unauthorized",
                               "Authentication required")
        token = token_match.group(1)

        try:
            identity = self._verifier.verify_access_token(token)
            if (not isinstance(identity, AuthenticatedFinanceIdentity) or
                not isinstance(identity.actor_id, UUID) or
                not isinstance(identity.allowed_tenants, frozenset) or
                tenant not in identity.allowed_tenants or
                not all(isinstance(t, UUID) for t in identity.allowed_tenants)):
                raise PermissionError("Invalid identity/tenant")
        except Exception:
            return self._reply(start_response, "403 Forbidden", "Access denied")

        try:
            # Use the exact tenant ID selected from verified entitlements,
            # NEVER a common DSN fallback or user-provided host/database.
            with closing(self._connections.open(tenant)) as db:
                result = render_private_dashboard_response(
                    db, raw_access_token=token,
                    identity_verifier=_VerifiedIdentity(identity),
                    requested_tenant=tenant, from_date=from_date, as_of=as_of,
                    currency=currency, locale=locale,
                )
            if not isinstance(result, PrivateFinanceResponse) or result.status != 200:
                raise RuntimeError("Unexpected reporting status")
            data = result.body.encode("utf-8")
            # A shared reverse proxy must also enforce TLS, no-store, request
            # limits, generic error pages and no Authorization header logging.
            headers = dict(_COMMON_HEADERS)
            headers["Content-Type"] = "text/html; charset=utf-8"
            headers["Content-Security-Policy"] = result.headers.get(
                "Content-Security-Policy", headers["Content-Security-Policy"]
            )
            headers["Content-Length"] = str(len(data))
            start_response("200 OK", list(headers.items()))
            return [data]
        except Exception:
            # Deliberately do not leak tenant names, DB errors, SQL or JWTs.
            return self._reply(start_response, "503 Service Unavailable",
                               "Report unavailable")
