"""Private, reference-only HTTP handoff for internal invoice/payment replay.

NOT a deployed endpoint. Inject a trusted RS256/OIDC verifier, a pinned tenant
DB registry and a server-side loader of approved, immutable financial sources.
Never construct a financial document or permissions from browser JSON.

This interface cannot perform bank transfers or ZATCA issuance. For a payment,
"applied" means only an internal allocation/journal has been posted.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import json
import re
from typing import Any, Mapping, Protocol
from uuid import UUID

from .accounting import Account, AccountingPeriod
from .finance_http_boundary import AuthenticatedFinanceIdentity, FinanceIdentityVerifier
from .finance_request_replay import (
    FinanceReplayReceipt, apply_internal_payment_once, issue_invoice_once,
)
from .finance_wsgi import TenantFinanceConnections, _COMMON_HEADERS
from .invoicing import InvoiceDocument, InvoiceLedgerAccounts
from .payments import OpenInvoice, PaymentLedgerAccounts, PaymentRequest
from .tenancy import Action, Membership, can_access


_PATH = "/api/v1/finance/internal-documents"
_ACTIONS = frozenset(("issue_internal_invoice", "apply_internal_payment"))
_BEARER = re.compile(r"Bearer ([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\Z")
_MAX_BODY = 512


class InvalidReplayCommand(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TrustedInvoiceSource:
    document: InvoiceDocument
    accounts: InvoiceLedgerAccounts
    chart: Mapping[str, Account]
    period: AccountingPeriod
    period_id: UUID
    memberships: tuple[Membership, ...]
    minor_units: int = 2
    goods_receipt_liability_accounts: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class TrustedPaymentSource:
    payment: PaymentRequest
    invoices: tuple[OpenInvoice, ...]
    accounts: PaymentLedgerAccounts
    chart: Mapping[str, Account]
    period: AccountingPeriod
    period_id: UUID
    memberships: tuple[Membership, ...]
    minor_units: int = 2


class ApprovedFinanceSourceLoader(Protocol):
    """Implement only from source-approved, server-owned records in chosen tenant DB.

    The same frozen historical financial request MUST remain retrievable for
    exact retries even after the first invoice is issued / payment is posted.
    Snapshots of outstanding invoices may legitimately differ on payment replay.
    """

    def load_invoice(
        self, connection, *, tenant_id: UUID, document_id: UUID, actor_id: UUID
    ) -> TrustedInvoiceSource:
        ...

    def load_payment(
        self, connection, *, tenant_id: UUID, document_id: UUID, actor_id: UUID
    ) -> TrustedPaymentSource:
        ...


def parse_internal_document_request(data: bytes) -> dict:
    """Only stable source ID, authorized tenant and allowlisted operation."""
    if not isinstance(data, bytes) or not 0 < len(data) <= _MAX_BODY:
        raise InvalidReplayCommand("Invalid content size")

    def unique(pairs):
        seen = {}
        for k, v in pairs:
            if k in seen:
                raise InvalidReplayCommand("Duplicate field")
            seen[k] = v
        return seen

    try:
        item = json.loads(data.decode("utf-8"), object_pairs_hook=unique)
    except (ValueError, UnicodeDecodeError, TypeError) as exc:
        raise InvalidReplayCommand("Malformed JSON") from exc
    if not isinstance(item, dict) or set(item) != {
        "action", "tenant", "document_id"
    } or item.get("action") not in _ACTIONS:
        raise InvalidReplayCommand("Unknown or missing command field")
    for field in ("tenant", "document_id"):
        value = item[field]
        if not isinstance(value, str) or len(value) != 36:
            raise InvalidReplayCommand("Expected canonical UUID")
        try:
            parsed = UUID(value)
        except ValueError as exc:
            raise InvalidReplayCommand("Invalid UUID") from exc
        if str(parsed) != value:
            raise InvalidReplayCommand("Expected lowercase canonical UUID")
        item[field] = parsed
    return item


def execute_verified_document_request(
    connection, *, identity: AuthenticatedFinanceIdentity,
    command: dict, source_loader: ApprovedFinanceSourceLoader,
) -> FinanceReplayReceipt:
    """Internal use only; HTTP does not own, create or mutate source payloads."""
    if (not isinstance(identity, AuthenticatedFinanceIdentity) or
        not isinstance(identity.actor_id, UUID) or
        not isinstance(identity.allowed_tenants, frozenset) or
        not all(isinstance(t, UUID) for t in identity.allowed_tenants) or
        command.get("action") not in _ACTIONS or
        not isinstance(command.get("tenant"), UUID) or
        not isinstance(command.get("document_id"), UUID) or
        set(command) != {"action", "tenant", "document_id"} or
        command["tenant"] not in identity.allowed_tenants):
        raise PermissionError("Verified source and tenant required")

    tenant, doc_id, actor = command["tenant"], command["document_id"], identity.actor_id
    # Source loading, membership validation, journal and request receipt all
    # participate in one outer PostgreSQL transaction.
    with connection.transaction():
        if command["action"] == "issue_internal_invoice":
            source = source_loader.load_invoice(
                connection, tenant_id=tenant, document_id=doc_id, actor_id=actor)
            if not isinstance(source, TrustedInvoiceSource):
                raise PermissionError("Trusted invoice source required")
            document = source.document
            kwargs = {
                "document": document, "accounts": source.accounts,
                "chart": source.chart, "period": source.period,
                "period_id": source.period_id, "actor_id": actor,
                "memberships": source.memberships,
                "minor_units": source.minor_units,
                "goods_receipt_liability_accounts":
                    source.goods_receipt_liability_accounts,
            }
            invoke = issue_invoice_once
        else:
            source = source_loader.load_payment(
                connection, tenant_id=tenant, document_id=doc_id, actor_id=actor)
            if not isinstance(source, TrustedPaymentSource):
                raise PermissionError("Trusted payment source required")
            document = source.payment
            kwargs = {
                "payment": document, "invoices": source.invoices,
                "accounts": source.accounts, "chart": source.chart,
                "period": source.period, "period_id": source.period_id,
                "actor_id": actor, "memberships": source.memberships,
                "minor_units": source.minor_units,
            }
            invoke = apply_internal_payment_once

        if (not isinstance(document, (InvoiceDocument, PaymentRequest)) or
            document.tenant_id != tenant or document.id != doc_id or
            not isinstance(source.period_id, UUID) or
            not isinstance(source.memberships, tuple) or
            not isinstance(source.minor_units, int) or
            not 0 <= source.minor_units <= 6 or
            not can_access(actor_id=actor, tenant_id=tenant,
                           action=Action.WRITE_ACCOUNTING,
                           memberships=source.memberships)):
            raise PermissionError("Approved document, accounting role and IDs required")
        receipt = invoke(connection, **kwargs)
        if (not isinstance(receipt, FinanceReplayReceipt) or
            receipt.document_id != doc_id or
            receipt.operation != (
                "internal_invoice" if command["action"] == "issue_internal_invoice"
                else "internal_payment"
            )):
            raise RuntimeError("Untrusted internal replay result")
        return receipt


class OneFinanceInternalDocumentsWSGI:
    """No cookies, URL tokens, user-supplied amounts/SQL or public CORS."""

    def __init__(
        self, *, verifier: FinanceIdentityVerifier,
        connections: TenantFinanceConnections,
        source_loader: ApprovedFinanceSourceLoader,
        allowed_hosts: frozenset[str],
    ):
        if (not isinstance(allowed_hosts, frozenset) or not allowed_hosts or
            any(not isinstance(host, str) or not host or host != host.lower()
                or "/" in host or host.startswith(".") for host in allowed_hosts)):
            raise ValueError("Explicit TLS host allowlist required")
        self._verifier, self._connections = verifier, connections
        self._source_loader, self._allowed_hosts = source_loader, allowed_hosts

    @staticmethod
    def _reply(start_response, status, body=None, allow=None):
        payload = json.dumps(
            body if body is not None else {"ok": False, "error": "Request unavailable"},
            ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        headers = dict(_COMMON_HEADERS)
        headers["Content-Type"] = "application/json; charset=utf-8"
        headers["Content-Length"] = str(len(payload))
        if allow:
            headers["Allow"] = allow
        start_response(status, list(headers.items()))
        return [payload]

    def __call__(self, environ, start_response):
        if (environ.get("wsgi.url_scheme") != "https" or
            environ.get("HTTP_HOST") not in self._allowed_hosts):
            return self._reply(start_response, "403 Forbidden")
        if environ.get("PATH_INFO") != _PATH:
            return self._reply(start_response, "404 Not Found")
        if environ.get("REQUEST_METHOD") != "POST":
            return self._reply(start_response, "405 Method Not Allowed", allow="POST")
        if environ.get("QUERY_STRING", "") or environ.get("HTTP_COOKIE", "") or \
                environ.get("HTTP_TRANSFER_ENCODING"):
            return self._reply(start_response, "400 Bad Request")
        if environ.get("CONTENT_TYPE") != "application/json":
            return self._reply(start_response, "415 Unsupported Media Type")
        length = environ.get("CONTENT_LENGTH")
        if not isinstance(length, str) or not length.isascii() or \
                not length.isdecimal() or len(length) > 4:
            return self._reply(start_response, "400 Bad Request")
        if int(length) > _MAX_BODY or int(length) <= 0:
            return self._reply(start_response, "413 Payload Too Large")
        try:
            raw = environ["wsgi.input"].read(int(length))
            if len(raw) != int(length):
                raise InvalidReplayCommand("Truncated body")
            command = parse_internal_document_request(raw)
        except (KeyError, ValueError, OSError, AttributeError, TypeError):
            return self._reply(start_response, "400 Bad Request")
        auth = environ.get("HTTP_AUTHORIZATION", "")
        if not isinstance(auth, str) or len(auth) > 8200:
            return self._reply(start_response, "401 Unauthorized")
        match = _BEARER.fullmatch(auth)
        if match is None:
            return self._reply(start_response, "401 Unauthorized")
        try:
            identity = self._verifier.verify_access_token(match.group(1))
            if (not isinstance(identity, AuthenticatedFinanceIdentity) or
                not isinstance(identity.actor_id, UUID) or
                not isinstance(identity.allowed_tenants, frozenset) or
                command["tenant"] not in identity.allowed_tenants):
                raise PermissionError("Unverified identity")
        except Exception:
            return self._reply(start_response, "403 Forbidden")
        try:
            with closing(self._connections.open(command["tenant"])) as conn:
                receipt = execute_verified_document_request(
                    conn, identity=identity, command=command,
                    source_loader=self._source_loader)
            return self._reply(start_response, "200 OK", {
                "ok": True, "document_id": str(receipt.document_id),
                "action": command["action"], "replayed": receipt.replayed,
            })
        except PermissionError:
            return self._reply(start_response, "403 Forbidden")
        except Exception:
            return self._reply(start_response, "409 Conflict")
