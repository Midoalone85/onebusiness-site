"""Minimal private WSGI *composition* for independent ONE ERP NEXT staging.

This is source code, NOT an Oracle deployment or TLS listener.
A trusted server must inject identity verification, physically pinned tenant
DB factories and an independently evidenced readiness probe. Unready staging
never touches tenant DBs. Static GitHub preview is not a backend.

By default, the dangerous internal invoice/payment POST route is NOT mounted.
This service never accepts unauthenticated SQL, arbitrary WSGI modules or
caller-controlled connection strings, and it has no banking/ZATCA integration.
"""
from __future__ import annotations

import json
from typing import Callable

from .finance_wsgi import OneFinanceWSGI, TenantFinanceConnections, _COMMON_HEADERS
from .finance_http_boundary import FinanceIdentityVerifier
from .finance_replay_commands_wsgi import (
    OneFinanceInternalDocumentsWSGI, ApprovedFinanceSourceLoader,
)
from .operations_read_wsgi import OneOperationsWSGI
from .invoice_approval_wsgi import OneInvoiceApprovalWSGI
from .staging_health import OneERPStagingHealthWSGI, StagingReadiness

_OPERATIONS = frozenset((
    "/api/v1/operations/sales",
    "/api/v1/operations/purchases",
    "/api/v1/operations/inventory",
    "/api/v1/operations/returns",
))
_HEALTH = frozenset(("/healthz", "/readyz"))
_REPORT = "/api/v1/finance/dashboard"
_AR_REPORT = "/api/v1/operations/receivables"
_COMMAND = "/api/v1/finance/internal-documents"
_APPROVAL = "/api/v1/finance/internal-invoice-approvals"


class OneERPStagingRouter:
    """Exact-path, denied-until-ready, backend-only WSGI dispatcher.

    No implicit/default tenant. Service connects only through a trusted
    TenantFinanceConnections instance, never request headers, URLs or cookies.
    All component WSGI routes independently enforce identity and HTTPS.
    """

    def __init__(
        self, *, allowed_hosts: frozenset[str], deployed_sha: str,
        verifier: FinanceIdentityVerifier,
        operations_connections: TenantFinanceConnections,
        finance_report_connections: TenantFinanceConnections,
        finance_write_connections: TenantFinanceConnections | None = None,
        approval_connections: TenantFinanceConnections | None = None,
        enable_invoice_source_approvals: bool = False,
        readiness_probe: Callable[[], StagingReadiness] | None = None,
        approved_source_loader: ApprovedFinanceSourceLoader | None = None,
        enable_internal_finance_writes: bool = False,
    ):
        if (not isinstance(operations_connections, TenantFinanceConnections) or
            not isinstance(finance_report_connections, TenantFinanceConnections) or
            operations_connections is finance_report_connections):
            raise ValueError("Independent pinned operation and finance reader registries required")
        if (type(enable_internal_finance_writes) is not bool or
            type(enable_invoice_source_approvals) is not bool):
            raise ValueError("Financial action activation must be explicit")
        if enable_invoice_source_approvals and (
            not isinstance(approval_connections,TenantFinanceConnections) or
            approval_connections in (operations_connections,finance_report_connections,
                                     finance_write_connections)
        ):
            raise ValueError("Independent approval SQL registry required")
        if not enable_invoice_source_approvals and approval_connections is not None:
            raise ValueError("Approval connections require explicit secure activation")
        if enable_internal_finance_writes and (
            approved_source_loader is None or
            not isinstance(finance_write_connections, TenantFinanceConnections) or
            finance_write_connections in (operations_connections, finance_report_connections)
        ):
            raise ValueError("Finance writes require isolated authorizer and write role registry")
        if not enable_internal_finance_writes and finance_write_connections is not None:
            raise ValueError("Write-capable tenant connection must not be mounted by default")
        self._health = OneERPStagingHealthWSGI(
            allowed_hosts=allowed_hosts, deployed_sha=deployed_sha,
            readiness_probe=readiness_probe,
        )
        self._hosts = allowed_hosts
        self._readiness_probe = readiness_probe
        self._operations = OneOperationsWSGI(
            verifier=verifier, connections=operations_connections, allowed_hosts=allowed_hosts,
        )
        # Confidential accounts-receivable details use the finance report
        # role, never the operational sales/stock reader SQL credentials.
        self._receivables = OneOperationsWSGI(
            verifier=verifier, connections=finance_report_connections,
            allowed_hosts=allowed_hosts,
        )
        self._finance = OneFinanceWSGI(
            verifier=verifier, connections=finance_report_connections, allowed_hosts=allowed_hosts,
        )
        self._approvals = None
        if enable_invoice_source_approvals:
            self._approvals = OneInvoiceApprovalWSGI(
                verifier=verifier,approval_connections=approval_connections,
                allowed_hosts=allowed_hosts,
            )
        self._commands = None
        if enable_internal_finance_writes:
            self._commands = OneFinanceInternalDocumentsWSGI(
                verifier=verifier, connections=finance_write_connections,
                source_loader=approved_source_loader, allowed_hosts=allowed_hosts,
            )

    @staticmethod
    def _reject(start_response, status: str):
        data = json.dumps({
            "ok": False, "error": "Service unavailable"
        }, separators=(",", ":")).encode("utf-8")
        headers = dict(_COMMON_HEADERS)
        headers["Content-Type"] = "application/json; charset=utf-8"
        headers["Content-Length"] = str(len(data))
        start_response(status, list(headers.items()))
        return [data]

    def __call__(self, environ, start_response):
        if (environ.get("wsgi.url_scheme") != "https" or
            environ.get("HTTP_HOST") not in self._hosts or
            environ.get("SCRIPT_NAME", "") not in ("", None)):
            return self._reject(start_response, "403 Forbidden")
        path = environ.get("PATH_INFO")
        if not isinstance(path, str):
            return self._reject(start_response, "404 Not Found")
        if path in _HEALTH:
            return self._health(environ, start_response)
        if (path not in _OPERATIONS and path != _REPORT and
            path != _AR_REPORT and
            (path != _COMMAND or self._commands is None) and
            (path != _APPROVAL or self._approvals is None)):
            return self._reject(start_response, "404 Not Found")
        # Readiness is a runtime precondition rather than just a UI color.
        # Both signed protected GET and explicitly activated internal POST
        # are denied if real provider/DB/restore/TLS evidence is unavailable.
        try:
            state = self._readiness_probe() if self._readiness_probe else None
            ready = isinstance(state, StagingReadiness) and state.is_ready()
        except Exception:
            ready = False
        if not ready:
            return self._reject(start_response, "503 Service Unavailable")
        if path in _OPERATIONS:
            return self._operations(environ, start_response)
        if path == _AR_REPORT:
            return self._receivables(environ, start_response)
        if path == _REPORT:
            return self._finance(environ, start_response)
        if path == _APPROVAL:
            return self._approvals(environ, start_response)
        return self._commands(environ, start_response)
