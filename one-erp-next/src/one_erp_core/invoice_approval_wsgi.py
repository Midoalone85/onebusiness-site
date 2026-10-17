"""Reference-only signed HTTPS maker-checker *approval* route.

This component approves an already-created immutable invoice source by ID.
It never creates the source, trusts a browser-supplied amount, issues a bank
payment, or sends a ZATCA invoice. This route is *not deployed* or mounted
on the staging router by default.

Requires a dedicated restricted approval SQL connection role, trusted issuer,
revocation and company entitlements, and a separate maker behind the backend.
"""
from __future__ import annotations

from contextlib import closing
import json
import re
from uuid import UUID

from .finance_http_boundary import AuthenticatedFinanceIdentity, FinanceIdentityVerifier
from .finance_wsgi import TenantFinanceConnections, _COMMON_HEADERS
from .invoice_source_workflow import independently_approve_invoice_source

_PATH="/api/v1/finance/internal-invoice-approvals"
_BEARER=re.compile(r"Bearer ([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\Z")
_MAX_BODY=512


def parse_invoice_approval_command(raw: bytes) -> tuple[UUID,UUID]:
    if not isinstance(raw,bytes) or not 0 < len(raw) <= _MAX_BODY:
        raise ValueError("Invalid approval payload")

    def unique_fields(pairs):
        fields={}
        for key,value in pairs:
            if key in fields:
                raise ValueError("Duplicate approval field")
            fields[key]=value
        return fields
    try:
        item=json.loads(raw.decode("utf-8"),object_pairs_hook=unique_fields)
    except (ValueError,UnicodeError) as exc:
        raise ValueError("Invalid approval JSON") from exc
    if not isinstance(item,dict) or set(item)!={"tenant","document_id"}:
        raise ValueError("Only approved reference fields accepted")
    result=[]
    for field in ("tenant","document_id"):
        value=item[field]
        if not isinstance(value,str) or len(value)!=36:
            raise ValueError("Canonical UUID required")
        try:
            identifier=UUID(value)
        except ValueError as exc:
            raise ValueError("Invalid UUID") from exc
        if str(identifier)!=value or identifier.int==0:
            raise ValueError("Canonical non-empty UUID required")
        result.append(identifier)
    return result[0],result[1]


class OneInvoiceApprovalWSGI:
    """A strictly scoped approval action for a signed *second* reviewer."""

    def __init__(
        self, *, verifier: FinanceIdentityVerifier,
        approval_connections: TenantFinanceConnections,
        allowed_hosts: frozenset[str],
    ):
        if (not isinstance(approval_connections,TenantFinanceConnections) or
            not isinstance(allowed_hosts,frozenset) or not allowed_hosts or
            any(not isinstance(h,str) or h!=h.lower()
                or not h or "/" in h or h.startswith(".") for h in allowed_hosts)):
            raise ValueError("Signed reviewer and approval role mapping required")
        self._verifier=verifier
        self._connections=approval_connections
        self._hosts=allowed_hosts

    @staticmethod
    def _reply(start_response,status,payload=None):
        value=payload if payload is not None else {
            "ok":False,"error":"Request unavailable"}
        raw=json.dumps(value,separators=(",",":")).encode("utf-8")
        headers=dict(_COMMON_HEADERS)
        headers["Content-Type"]="application/json; charset=utf-8"
        headers["Content-Length"]=str(len(raw))
        start_response(status,list(headers.items()))
        return [raw]

    def __call__(self,environ,start_response):
        if (environ.get("wsgi.url_scheme")!="https" or
            environ.get("HTTP_HOST") not in self._hosts):
            return self._reply(start_response,"403 Forbidden")
        if environ.get("PATH_INFO")!=_PATH:
            return self._reply(start_response,"404 Not Found")
        if environ.get("REQUEST_METHOD")!="POST":
            return self._reply(start_response,"405 Method Not Allowed")
        if (environ.get("QUERY_STRING","") or
            environ.get("HTTP_COOKIE","") or
            environ.get("HTTP_TRANSFER_ENCODING") or
            environ.get("CONTENT_TYPE")!="application/json"):
            return self._reply(start_response,"400 Bad Request")
        length=environ.get("CONTENT_LENGTH","")
        if (not isinstance(length,str) or not length.isascii() or
            not length.isdecimal() or len(length)>4 or
            not 0<int(length)<=_MAX_BODY):
            return self._reply(start_response,"400 Bad Request")
        try:
            raw=environ["wsgi.input"].read(int(length))
            if len(raw)!=int(length):
                raise ValueError("Short approval body")
            tenant,doc=parse_invoice_approval_command(raw)
        except (ValueError,TypeError,KeyError,OSError,AttributeError):
            return self._reply(start_response,"400 Bad Request")
        auth=environ.get("HTTP_AUTHORIZATION","")
        if not isinstance(auth,str) or len(auth)>8200:
            return self._reply(start_response,"401 Unauthorized")
        match=_BEARER.fullmatch(auth)
        if not match:
            return self._reply(start_response,"401 Unauthorized")
        try:
            identity=self._verifier.verify_access_token(match.group(1))
            if (not isinstance(identity,AuthenticatedFinanceIdentity) or
                not isinstance(identity.actor_id,UUID) or
                not isinstance(identity.allowed_tenants,frozenset) or
                not all(isinstance(t,UUID) for t in identity.allowed_tenants) or
                tenant not in identity.allowed_tenants):
                raise PermissionError("Untrusted reviewer")
        except Exception:
            return self._reply(start_response,"403 Forbidden")
        try:
            with closing(self._connections.open(tenant)) as connection:
                independently_approve_invoice_source(
                    connection, identity=identity,tenant_id=tenant,
                    document_id=doc)
        except PermissionError:
            return self._reply(start_response,"403 Forbidden")
        except Exception:
            # Hide duplicate keys, missing resources, SQL secrets and DSNs.
            return self._reply(start_response,"409 Conflict")
        return self._reply(start_response,"200 OK",{
            "ok":True,"tenant":str(tenant),"document_id":str(doc),
            "status":"approved_internal_source",
        })
