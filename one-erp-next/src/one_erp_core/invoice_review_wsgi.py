"""Private HTTPS GET reviewer evidence route; source-only, disabled by default.

No browser-selected SQL/actor/role. Uses verified JWT and a dedicated
nonprivileged read-only tenant registry. The POST approval remains independent
and disabled unless a separate operator explicitly enables it.
"""
from __future__ import annotations

from contextlib import closing
import json
import re
from urllib.parse import parse_qs
from uuid import UUID

from .finance_http_boundary import AuthenticatedFinanceIdentity, FinanceIdentityVerifier
from .finance_wsgi import TenantFinanceConnections, _COMMON_HEADERS
from .invoice_review_source import load_invoice_review_summary

_PATH="/api/v1/finance/internal-invoice-source"
_TOKEN=re.compile(r"Bearer ([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\Z")
_UUID=re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")


class OneInvoiceReviewWSGI:
    def __init__(self, *, verifier: FinanceIdentityVerifier,
                 review_connections: TenantFinanceConnections,
                 allowed_hosts: frozenset[str]):
        if (not isinstance(review_connections,TenantFinanceConnections) or
            not isinstance(allowed_hosts,frozenset) or not allowed_hosts or
            any(not isinstance(h,str) or h!=h.lower() or
                not h or "/" in h or h.startswith(".") for h in allowed_hosts)):
            raise ValueError("Pinned invoice review reader configuration required")
        self._verifier=verifier
        self._connections=review_connections
        self._hosts=allowed_hosts

    @staticmethod
    def _reply(start_response,status,payload):
        body=json.dumps(payload,ensure_ascii=False,
                        allow_nan=False,separators=(",",":")).encode("utf-8")
        headers=dict(_COMMON_HEADERS)
        headers["Content-Type"]="application/json; charset=utf-8"
        headers["Content-Length"]=str(len(body))
        start_response(status,list(headers.items()))
        return [body]

    def __call__(self,environ,start_response):
        fail=lambda status:self._reply(
            start_response,status,{"ok":False,"error":"Review unavailable"})
        if (environ.get("wsgi.url_scheme")!="https" or
            environ.get("HTTP_HOST") not in self._hosts or
            environ.get("SCRIPT_NAME","") not in ("",None)):
            return fail("403 Forbidden")
        if environ.get("PATH_INFO")!=_PATH:
            return fail("404 Not Found")
        if environ.get("REQUEST_METHOD")!="GET":
            return fail("405 Method Not Allowed")
        if (environ.get("CONTENT_LENGTH","") not in ("","0") or
            environ.get("HTTP_COOKIE","") or
            environ.get("HTTP_TRANSFER_ENCODING")):
            return fail("400 Bad Request")
        query=environ.get("QUERY_STRING","")
        if not isinstance(query,str) or not 0<len(query)<=160:
            return fail("400 Bad Request")
        try:
            args=parse_qs(query,keep_blank_values=True,strict_parsing=True,
                          max_num_fields=2)
            if set(args)!={"tenant","document_id"} or any(len(v)!=1 for v in args.values()):
                raise ValueError("Only exact invoice references permitted")
            raw_tenant=args["tenant"][0];raw_doc=args["document_id"][0]
            if not _UUID.fullmatch(raw_tenant) or not _UUID.fullmatch(raw_doc):
                raise ValueError("Canonical nonnil UUIDs required")
            tenant,document=UUID(raw_tenant),UUID(raw_doc)
            if tenant.int==0 or document.int==0:
                raise ValueError("Nonnil UUIDs required")
        except (ValueError,TypeError,OverflowError):
            return fail("400 Bad Request")
        header=environ.get("HTTP_AUTHORIZATION","")
        if not isinstance(header,str) or len(header)>8200:
            return fail("401 Unauthorized")
        match=_TOKEN.fullmatch(header)
        if not match:
            return fail("401 Unauthorized")
        try:
            identity=self._verifier.verify_access_token(match.group(1))
            if (not isinstance(identity,AuthenticatedFinanceIdentity) or
                not isinstance(identity.actor_id,UUID) or
                not isinstance(identity.allowed_tenants,frozenset) or
                not all(isinstance(t,UUID) for t in identity.allowed_tenants) or
                tenant not in identity.allowed_tenants):
                raise PermissionError("Untrusted reviewer")
        except Exception:
            return fail("403 Forbidden")
        try:
            with closing(self._connections.open(tenant)) as connection:
                evidence=load_invoice_review_summary(
                    connection,tenant_id=tenant,actor_id=identity.actor_id,
                    document_id=document,
                )
            if (not isinstance(evidence,dict) or
                evidence.get("tenant")!=str(tenant) or
                evidence.get("document_id")!=str(document) or
                evidence.get("reviewable") is not True):
                raise PermissionError("Mismatched review evidence")
        except PermissionError:
            return fail("403 Forbidden")
        except Exception:
            return fail("503 Service Unavailable")
        return self._reply(start_response,"200 OK",evidence)
