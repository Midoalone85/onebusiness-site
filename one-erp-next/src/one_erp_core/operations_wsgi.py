"""HTTPS-only WSGI JSON API for ONE ERP NEXT operations workspaces.

Integrates the existing verified finance identity adapter and exact server
tenant->connection mapping. Never trusts actor, database URI or permissions
from query/body. Does not implement transaction writes, login or deployment.
"""
from __future__ import annotations

from contextlib import closing
import json
import re
from urllib.parse import parse_qs
from uuid import UUID

from .finance_http_boundary import AuthenticatedFinanceIdentity
from .finance_wsgi import TenantFinanceConnections
from .operations_read_model import MODULES, read_operations_page

_ROUTE = re.compile(r"^/api/v1/operations/(sales|purchases|inventory|returns|refunds)$")
_BEARER = re.compile(r"^Bearer ([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)$")
_HEADERS = (
    ("Content-Type","application/json; charset=utf-8"),
    ("Cache-Control","private, no-store, max-age=0"),
    ("Pragma","no-cache"),
    ("X-Content-Type-Options","nosniff"),
    ("X-Frame-Options","DENY"),
    ("Referrer-Policy","no-referrer"),
    ("Vary","Authorization"),
    ("Content-Security-Policy","default-src 'none'; frame-ancestors 'none'"),
)


class OneOperationsWSGI:
    """Bespoke strict read-only route. Production adapter must attach real IdP."""

    def __init__(self, *, verifier, connections: TenantFinanceConnections,
                 allowed_hosts: frozenset[str]):
        if (not isinstance(allowed_hosts,frozenset) or not allowed_hosts
            or any(not isinstance(h,str) or not h or h!=h.lower()
                   or "/" in h or h.startswith(".") for h in allowed_hosts)):
            raise ValueError("Explicit HTTPS host allowlist required")
        self._verifier=verifier
        self._connections=connections
        self._allowed_hosts=allowed_hosts

    @staticmethod
    def _reply(start_response, code: str, data):
        body=json.dumps(data,ensure_ascii=False,allow_nan=False,
                        separators=(",",":")).encode("utf-8")
        start_response(code,list(_HEADERS)+[("Content-Length",str(len(body)))])
        return [body]

    def __call__(self,environ,start_response):
        if environ.get("wsgi.url_scheme")!="https":
            return self._reply(start_response,"403 Forbidden",{"error":"HTTPS required"})
        if environ.get("HTTP_HOST") not in self._allowed_hosts:
            return self._reply(start_response,"403 Forbidden",{"error":"Forbidden"})
        path=environ.get("PATH_INFO","")
        matched=_ROUTE.fullmatch(path) if isinstance(path,str) else None
        if matched is None:
            return self._reply(start_response,"404 Not Found",{"error":"Not found"})
        if environ.get("REQUEST_METHOD")!="GET":
            return self._reply(start_response,"405 Method Not Allowed",
                               {"error":"Method not allowed"})
        if environ.get("CONTENT_LENGTH","") not in ("","0"):
            return self._reply(start_response,"400 Bad Request",
                               {"error":"Request body forbidden"})
        query=environ.get("QUERY_STRING","")
        if not isinstance(query,str) or len(query)>130:
            return self._reply(start_response,"400 Bad Request",
                               {"error":"Invalid query"})
        try:
            args=parse_qs(query,keep_blank_values=True,strict_parsing=True,
                          max_num_fields=2)
            if set(args)!={"tenant","page"} or any(len(v)!=1 for v in args.values()):
                raise ValueError("Query shape mismatch")
            tenant=UUID(args["tenant"][0])
            raw_page=args["page"][0]
            if not re.fullmatch(r"0|[1-9][0-9]{0,2}",raw_page):
                raise ValueError("Invalid page")
            page=int(raw_page)
            if page>100:
                raise ValueError("Maximum page exceeded")
        except (ValueError,KeyError,OverflowError):
            return self._reply(start_response,"400 Bad Request",
                               {"error":"Invalid query"})
        header=environ.get("HTTP_AUTHORIZATION","")
        match=_BEARER.fullmatch(header) if isinstance(header,str) and len(header)<=8200 else None
        if match is None:
            return self._reply(start_response,"401 Unauthorized",
                               {"error":"Bearer token required"})
        try:
            identity=self._verifier.verify_access_token(match.group(1))
            if (not isinstance(identity,AuthenticatedFinanceIdentity)
                or not isinstance(identity.actor_id,UUID)
                or not isinstance(identity.allowed_tenants,frozenset)
                or tenant not in identity.allowed_tenants
                or not all(isinstance(t,UUID) for t in identity.allowed_tenants)):
                raise PermissionError("Not an authorized tenant")
        except Exception:
            return self._reply(start_response,"403 Forbidden",
                               {"error":"Access denied"})
        try:
            with closing(self._connections.open(tenant)) as connection:
                payload=read_operations_page(connection,tenant_id=tenant,
                     actor_id=identity.actor_id,module=matched.group(1),
                     page=page)
            if payload.get("tenant_id")!=str(tenant) or payload.get("module")!=matched.group(1):
                raise ValueError("Unexpected tenant/module in result")
            return self._reply(start_response,"200 OK",payload)
        except Exception:
            # Never reveal tenant, exception, SQL, credentials or JWT.
            return self._reply(start_response,"503 Service Unavailable",
                               {"error":"Workspace unavailable"})
