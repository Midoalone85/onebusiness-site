"""Private deployment-owned staging health and readiness contract.

This application is a standalone WSGI building block, NOT a deployed server.
The trusted server supplies its exact deployed commit SHA plus a callback
that independently checks every readiness prerequisite at query time.
No tenant, bank, password, database address or token is returned.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Callable, Mapping

from .finance_wsgi import _COMMON_HEADERS

_SHA = re.compile(r"[0-9a-f]{40}\Z")
_CHECKS = frozenset((
    "trusted_identity_provider",
    "isolated_tenant_routing",
    "restricted_database_roles",
    "approved_migrations",
    "restored_backup_verified",
    "live_https_certificate",
))


@dataclass(frozen=True, slots=True)
class StagingReadiness:
    """Explicit, independent probe facts, not client-supplied checkbox claims."""
    checks: Mapping[str, bool]

    def is_ready(self) -> bool:
        return (
            isinstance(self.checks, Mapping) and
            set(self.checks) == _CHECKS and
            all(self.checks.get(name) is True for name in _CHECKS)
        )


class OneERPStagingHealthWSGI:
    """GET-only HTTPS/host-pinned health surface with fail-closed readiness.

    The readiness_probe must be controlled by the deployed operator and use
    real provider/database/restore evidence. Do not inject an all-true probe
    merely to make the UI green. Unknown fields and exceptions deny readiness.
    """

    def __init__(
        self, *, allowed_hosts: frozenset[str], deployed_sha: str,
        readiness_probe: Callable[[], StagingReadiness] | None = None,
    ):
        if (not isinstance(allowed_hosts, frozenset) or not allowed_hosts or
            any(not isinstance(host, str) or not host or
                host != host.lower() or "/" in host or
                host.startswith(".") for host in allowed_hosts) or
            not isinstance(deployed_sha, str) or not _SHA.fullmatch(deployed_sha) or
            (readiness_probe is not None and not callable(readiness_probe))):
            raise ValueError("Trusted staging host and verified commit required")
        self._allowed_hosts = allowed_hosts
        self._deployed_sha = deployed_sha
        self._readiness_probe = readiness_probe

    @staticmethod
    def _reply(start_response, status: str, payload: dict):
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = dict(_COMMON_HEADERS)
        headers["Content-Type"] = "application/json; charset=utf-8"
        headers["Content-Length"] = str(len(raw))
        start_response(status, list(headers.items()))
        return [raw]

    def __call__(self, env, start_response):
        # WSGI scheme must be set by trusted TLS terminator configuration,
        # never by a caller's X-Forwarded-Proto header.
        if (env.get("wsgi.url_scheme") != "https" or
            env.get("HTTP_HOST") not in self._allowed_hosts):
            return self._reply(start_response, "403 Forbidden", {"ok": False})
        if env.get("PATH_INFO") not in ("/healthz", "/readyz"):
            return self._reply(start_response, "404 Not Found", {"ok": False})
        if env.get("REQUEST_METHOD") != "GET":
            return self._reply(start_response, "405 Method Not Allowed", {"ok": False})
        if (env.get("QUERY_STRING", "") or env.get("HTTP_COOKIE", "") or
            env.get("CONTENT_LENGTH", "") not in ("", "0") or
            env.get("HTTP_AUTHORIZATION", "") not in ("", None)):
            return self._reply(start_response, "400 Bad Request", {"ok": False})
        if env["PATH_INFO"] == "/healthz":
            return self._reply(start_response, "200 OK", {
                "service": "one-erp-next",
                "environment": "staging",
                "commit_sha": self._deployed_sha,
            })
        ready = False
        if self._readiness_probe is not None:
            try:
                outcome = self._readiness_probe()
                ready = isinstance(outcome, StagingReadiness) and outcome.is_ready()
            except Exception:
                ready = False
        if not ready:
            return self._reply(start_response, "503 Service Unavailable", {
                "ready": False,
            })
        return self._reply(start_response, "200 OK", {"ready": True})
