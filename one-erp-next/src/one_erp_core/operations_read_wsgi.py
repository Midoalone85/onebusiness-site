"""ONE ERP NEXT / 056 private operational JSON endpoint (NOT deployed).

Serve only behind an independently authenticated HTTPS reverse proxy, backed
by a vetted OIDC identity verifier and tenant-scoped PostgreSQL connections.
This prototype *never* accepts caller-selected DSN, actor ID or SQL statement.
No CORS, browser token in URL, public caching or mutation route is supported.
"""
from __future__ import annotations

from contextlib import closing
import json
import re
from urllib.parse import parse_qs
from uuid import UUID

from .finance_http_boundary import (
    AuthenticatedFinanceIdentity, FinanceIdentityVerifier,
)
from .finance_wsgi import TenantFinanceConnections
from .operations_read_repository import load_operations_page

_ALLOWED = frozenset(("sales","purchases","inventory","returns"))
_PATH_PREFIX = "/api/v1/operations/"
_TOKEN = re.compile(r"^Bearer ([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)$")
_HOST = re.compile(r"^[a-z0-9][a-z0-9.-]{0,249}(?::[0-9]{2,5})?$")
_NUMBER = re.compile(r"^(?:0|[1-9][0-9]{0,5})$")
_HEADERS = (
    ("Cache-Control","private, no-store, max-age=0"),
    ("Pragma","no-cache"),
    ("X-Content-Type-Options","nosniff"),
    ("X-Frame-Options","DENY"),
    ("Referrer-Policy","no-referrer"),
    ("Content-Security-Policy",
     "default-src 'none'; base-uri 'none'; object-src 'none'; "
     "frame-ancestors 'none'; form-action 'none'"),
    ("Vary","Authorization"),
)


class OneOperationsWSGI:
    """Explicit read-only HTTPS GET routes for company business module pages."""

    def __init__(
        self, *, verifier: FinanceIdentityVerifier,
        connections: TenantFinanceConnections,
        allowed_hosts: frozenset[str],
    ):
        if (not isinstance(allowed_hosts, frozenset) or not allowed_hosts
            or any(not isinstance(host,str) or
                   not _HOST.fullmatch(host) or ".." in host
                   for host in allowed_hosts)):
            raise ValueError("Trusted HTTPS host allowlist required")
        self._verifier = verifier
        self._connections = connections
        self._hosts = allowed_hosts

    @staticmethod
    def _response(start_response, status: str, payload: dict):
        body=json.dumps(payload,ensure_ascii=False,
                        allow_nan=False,separators=(",",":")).encode("utf-8")
        headers=list(_HEADERS)+[
            ("Content-Type","application/json; charset=utf-8"),
            ("Content-Length",str(len(body))),
        ]
        start_response(status,headers)
        return [body]

    @staticmethod
    def _fail(start_response,status: str, code: str):
        return OneOperationsWSGI._response(
            start_response,status,{"error":code})

    def __call__(self, environ, start_response):
        if environ.get("wsgi.url_scheme")!="https":
            return self._fail(start_response,"403 Forbidden","https_required")
        if environ.get("HTTP_HOST") not in self._hosts:
            return self._fail(start_response,"403 Forbidden","access_denied")
        if environ.get("REQUEST_METHOD")!="GET":
            return self._fail(start_response,"405 Method Not Allowed","read_only")
        path=environ.get("PATH_INFO")
        if (not isinstance(path,str) or not path.startswith(_PATH_PREFIX)
            or path[len(_PATH_PREFIX):] not in _ALLOWED):
            return self._fail(start_response,"404 Not Found","not_found")
        if environ.get("CONTENT_LENGTH","") not in ("","0"):
            return self._fail(start_response,"400 Bad Request","invalid_request")
        query=environ.get("QUERY_STRING","")
        if not isinstance(query,str) or len(query)>256:
            return self._fail(start_response,"400 Bad Request","invalid_request")
        try:
            params=parse_qs(
                query,strict_parsing=True,keep_blank_values=True,max_num_fields=3)
            if set(params)-{"tenant","limit","offset"} or "tenant" not in params:
                raise ValueError("Unexpected query keys")
            if any(len(v)!=1 for v in params.values()):
                raise ValueError("Repeated query field")
            requested_tenant=UUID(params["tenant"][0])
            size=params.get("limit",["25"])[0]
            page=params.get("offset",["0"])[0]
            if not _NUMBER.fullmatch(size) or not _NUMBER.fullmatch(page):
                raise ValueError("Non decimal paging field")
            limit,offset=int(size),int(page)
            if not 1<=limit<=50 or not 0<=offset<=5000:
                raise ValueError("Page size exceeds policy")
        except (ValueError,KeyError,OverflowError,TypeError):
            return self._fail(start_response,"400 Bad Request","invalid_request")
        header=environ.get("HTTP_AUTHORIZATION","")
        if not isinstance(header,str) or len(header)>8200:
            return self._fail(start_response,"401 Unauthorized","authentication_required")
        token=_TOKEN.fullmatch(header)
        if not token:
            return self._fail(start_response,"401 Unauthorized","authentication_required")
        try:
            identity=self._verifier.verify_access_token(token.group(1))
            if (not isinstance(identity,AuthenticatedFinanceIdentity) or
                not isinstance(identity.actor_id,UUID) or
                not isinstance(identity.allowed_tenants,frozenset) or
                requested_tenant not in identity.allowed_tenants or
                not all(isinstance(x,UUID) for x in identity.allowed_tenants)):
                raise PermissionError("Company not authorized")
        except Exception:
            return self._fail(start_response,"403 Forbidden","access_denied")
        try:
            db=self._connections.open(requested_tenant)
        except Exception:
            # Missing/disabled tenant database is an infrastructure state,
            # not a valid authorization decision or cross-tenant fallback.
            return self._fail(start_response,"503 Service Unavailable",
                              "temporarily_unavailable")
        try:
            with closing(db):
                page_result=load_operations_page(
                    db,tenant_id=requested_tenant,actor_id=identity.actor_id,
                    module=path[len(_PATH_PREFIX):],limit=limit,offset=offset)
        except PermissionError:
            return self._fail(start_response,"403 Forbidden","access_denied")
        except Exception:
            # Never disclose tenant existence, source SQL, keys or DSNs.
            return self._fail(start_response,"503 Service Unavailable",
                              "temporarily_unavailable")
        return self._response(start_response,"200 OK",page_result)
