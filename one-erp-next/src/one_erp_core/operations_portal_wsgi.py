"""ONE ERP NEXT operational web shell + authenticated API dispatcher.

Self-contained assets use no external CDN and no fictitious financial rows.
Static shell contains zero sensitive data and never stores access tokens.
JWT verification and exact tenant DB routing happen only in JSON API.
"""
from __future__ import annotations

from pathlib import Path

from .operations_wsgi import OneOperationsWSGI

_ASSETS={
    "/assets/operations.css":("operations.css","text/css; charset=utf-8"),
    "/assets/operations.js":("operations.js","text/javascript; charset=utf-8"),
}
_SHELL=Path(__file__).resolve().parents[2]/"web"
_MODULES=frozenset({"sales","purchases","inventory","returns","refunds"})
_STATIC_CSP=(
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; img-src 'self'; font-src 'self'; "
    "base-uri 'none'; object-src 'none'; frame-ancestors 'none'; "
    "form-action 'self'"
)


class OneOperationsPortalWSGI:
    """One HTTPS host with strict public static assets and private JSON APIs."""
    def __init__(self,*,verifier,connections,allowed_hosts:frozenset[str]):
        self._api=OneOperationsWSGI(
            verifier=verifier,connections=connections,
            allowed_hosts=allowed_hosts,
        )
        self._hosts=allowed_hosts

    @staticmethod
    def _text(start_response,status,content,content_type):
        start_response(status,[
            ("Content-Type",content_type),("Content-Length",str(len(content))),
            ("Cache-Control","no-store, max-age=0"),
            ("X-Content-Type-Options","nosniff"),
            ("X-Frame-Options","DENY"),
            ("Referrer-Policy","no-referrer"),
            ("Content-Security-Policy",_STATIC_CSP),
        ])
        return [content]

    def __call__(self,environ,start_response):
        path=environ.get("PATH_INFO","")
        if isinstance(path,str) and path.startswith("/api/v1/operations/"):
            return self._api(environ,start_response)
        if environ.get("wsgi.url_scheme")!="https" or \
           environ.get("HTTP_HOST") not in self._hosts:
            return self._text(start_response,"403 Forbidden",
                              b"Forbidden","text/plain; charset=utf-8")
        if environ.get("REQUEST_METHOD")!="GET":
            return self._text(start_response,"405 Method Not Allowed",
                              b"Method not allowed","text/plain; charset=utf-8")
        if environ.get("CONTENT_LENGTH","") not in ("","0") or \
           environ.get("QUERY_STRING","") not in ("",None):
            return self._text(start_response,"400 Bad Request",
                              b"Invalid request","text/plain; charset=utf-8")
        if path in ("/app","/app/"):
            name,typ="operations.html","text/html; charset=utf-8"
        elif isinstance(path,str) and path.startswith("/app/") and \
             path[5:] in _MODULES:
            name,typ="operations.html","text/html; charset=utf-8"
        elif path in _ASSETS:
            name,typ=_ASSETS[path]
        else:
            return self._text(start_response,"404 Not Found",
                              b"Not found","text/plain; charset=utf-8")
        try:
            payload=(_SHELL/name).read_bytes()
        except OSError:
            return self._text(start_response,"503 Service Unavailable",
                              b"Portal unavailable","text/plain; charset=utf-8")
        return self._text(start_response,"200 OK",payload,typ)
