"""Private authenticated read-only activity timeline endpoint, NOT deployed.

GET /api/v1/activity/timeline?tenant=...&record_type=invoice&record_id=...
Identity, permission and tenant DB are NEVER chosen from browser actor claims.
"""
from __future__ import annotations

from contextlib import closing
import json
from urllib.parse import parse_qs
from uuid import UUID

from .activity_repository import ACTIVITY_TYPES,load_record_activity
from .finance_http_boundary import AuthenticatedFinanceIdentity
from .finance_wsgi import TenantFinanceConnections
from .operations_read_wsgi import _HEADERS,_HOST,_NUMBER,_TOKEN

_PATH="/api/v1/activity/timeline"


class OneActivityWSGI:
    def __init__(self, *, verifier, connections: TenantFinanceConnections,
                 allowed_hosts: frozenset[str]):
        if (not isinstance(connections,TenantFinanceConnections) or
            not isinstance(allowed_hosts,frozenset) or not allowed_hosts or
            any(not isinstance(host,str) or not _HOST.fullmatch(host)
                or ".." in host for host in allowed_hosts)):
            raise ValueError("Independent trusted activity reader required")
        self._verifier=verifier
        self._connections=connections
        self._hosts=allowed_hosts

    @staticmethod
    def _response(start_response,status,payload):
        data=json.dumps(payload,ensure_ascii=False,
                        allow_nan=False,separators=(",",":")).encode("utf-8")
        start_response(status,list(_HEADERS)+[
            ("Content-Type","application/json; charset=utf-8"),
            ("Content-Length",str(len(data))),
        ])
        return [data]

    def __call__(self,environ,start_response):
        def fail(status,code):
            return self._response(start_response,status,{"error":code})
        if environ.get("wsgi.url_scheme")!="https" or environ.get("HTTP_HOST") not in self._hosts:
            return fail("403 Forbidden","access_denied")
        if environ.get("PATH_INFO")!=_PATH:
            return fail("404 Not Found","not_found")
        if environ.get("REQUEST_METHOD")!="GET":
            return fail("405 Method Not Allowed","read_only")
        if environ.get("CONTENT_LENGTH","") not in ("","0"):
            return fail("400 Bad Request","invalid_request")
        query=environ.get("QUERY_STRING","")
        if not isinstance(query,str) or len(query)>400:
            return fail("400 Bad Request","invalid_request")
        try:
            fields=parse_qs(query,strict_parsing=True,keep_blank_values=True,max_num_fields=5)
            if not {"tenant","record_type","record_id"}.issubset(fields):
                raise ValueError("Required document context missing")
            if set(fields)-{"tenant","record_type","record_id","limit","offset"}:
                raise ValueError("Unexpected query")
            if any(len(value)!=1 for value in fields.values()):
                raise ValueError("Repeated key")
            tenant=UUID(fields["tenant"][0])
            record_id=UUID(fields["record_id"][0])
            if (str(tenant)!=fields["tenant"][0] or str(record_id)!=fields["record_id"][0]
                or tenant.int==0 or record_id.int==0):
                raise ValueError("Canonical UUIDs required")
            record_type=fields["record_type"][0]
            if record_type not in ACTIVITY_TYPES:
                raise ValueError("Unknown auditable record type")
            limit_str=fields.get("limit",["20"])[0]
            offset_str=fields.get("offset",["0"])[0]
            if not _NUMBER.fullmatch(limit_str) or not _NUMBER.fullmatch(offset_str):
                raise ValueError("Invalid pagination")
            limit,offset=int(limit_str),int(offset_str)
            if not 1<=limit<=50 or not 0<=offset<=5000:
                raise ValueError("Pagination out of range")
        except (ValueError,KeyError,TypeError,OverflowError):
            return fail("400 Bad Request","invalid_request")
        header=environ.get("HTTP_AUTHORIZATION","")
        if not isinstance(header,str) or len(header)>8200:
            return fail("401 Unauthorized","authentication_required")
        token=_TOKEN.fullmatch(header)
        if not token:
            return fail("401 Unauthorized","authentication_required")
        try:
            identity=self._verifier.verify_access_token(token.group(1))
            if (not isinstance(identity,AuthenticatedFinanceIdentity)
                or not isinstance(identity.actor_id,UUID)
                or not isinstance(identity.allowed_tenants,frozenset)
                or tenant not in identity.allowed_tenants
                or not all(isinstance(t,UUID) for t in identity.allowed_tenants)):
                raise PermissionError("No signed company permission")
        except Exception:
            return fail("403 Forbidden","access_denied")
        try:
            db=self._connections.open(tenant)
        except Exception:
            return fail("503 Service Unavailable","temporarily_unavailable")
        try:
            with closing(db):
                result=load_record_activity(
                    db,tenant_id=tenant,actor_id=identity.actor_id,
                    record_type=record_type,record_id=record_id,
                    limit=limit,offset=offset)
        except PermissionError:
            return fail("403 Forbidden","access_denied")
        except Exception:
            return fail("503 Service Unavailable","temporarily_unavailable")
        return self._response(start_response,"200 OK",result)
