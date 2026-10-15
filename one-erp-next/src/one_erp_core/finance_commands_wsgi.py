"""Authenticated, allowlisted finance COMMANDS for ONE ERP NEXT.

Use only behind a trusted HTTPS WSGI server with RS256 verifier, authoritative
server-side subject/tenant entitlements, and backend-only PostgreSQL connections.
This is NOT an independently deployed website. Every route authenticates before
opening a database and runs explicit SQL functions under server-bound actor GUCs.

Do not give browsers database access or unrestricted SQL. PostgreSQL GUCs are
NOT independently secure authentication if a caller can execute arbitrary SQL.
This pilot retains the disposable CI's broad DB role grants until operations
receive deployment-reviewed least-privilege service roles.
"""
from __future__ import annotations

from contextlib import closing
from datetime import date
from decimal import Decimal, InvalidOperation
import json
import re
from typing import Any
from uuid import UUID

from .finance_http_boundary import AuthenticatedFinanceIdentity, FinanceIdentityVerifier
from .finance_wsgi import TenantFinanceConnections, _COMMON_HEADERS

_PATH = "/api/v1/finance/commands"
_BEARER = re.compile(r"Bearer ([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\Z")
_CURRENCY = re.compile(r"[A-Z]{3}\Z")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_MAX_BODY_BYTES = 8192
_ALLOWED_FIELDS = {
    "request_soft_close": frozenset({"action", "tenant", "period_id"}),
    "approve_soft_close": frozenset({"action", "tenant", "period_id"}),
    "request_soft_adjustment": frozenset({
        "action", "tenant", "period_id", "request_id", "journal_id",
        "occurred_on", "currency", "lines", "reason",
    }),
    "approve_soft_adjustment": frozenset({"action", "tenant", "request_id"}),
    "post_soft_adjustment": frozenset({"action", "tenant", "journal_id"}),
}
_SQL = {
    "request_soft_close": "SELECT public.one_request_period_soft_close(%s,%s,%s)",
    "approve_soft_close": "SELECT public.one_approve_period_soft_close(%s,%s,%s)",
    "request_soft_adjustment": (
        "SELECT public.one_request_soft_adjustment_lines("
        "%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)"
    ),
    "approve_soft_adjustment": "SELECT public.one_approve_soft_adjustment(%s,%s,%s)",
    "post_soft_adjustment": "SELECT public.one_post_journal(%s,%s,%s)",
}
_POST_CHECK = (
    "SELECT 1 FROM public.one_soft_adjustment_requests r "
    "JOIN public.one_soft_adjustment_approvals a ON "
    "a.tenant_id=r.tenant_id AND a.request_id=r.id "
    "WHERE r.tenant_id=%s AND r.journal_id=%s AND r.requested_by=%s"
)


class InvalidCommand(ValueError):
    """Invalid client syntax. Never include client contents in error bodies."""


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidCommand("Repeated JSON property")
        result[key] = value
    return result


def _invalid_constant(_):
    raise InvalidCommand("Non-finite number")


def _uuid(value: Any) -> UUID:
    if not isinstance(value, str) or len(value) != 36:
        raise InvalidCommand("Invalid UUID")
    try:
        result = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise InvalidCommand("Invalid UUID") from exc
    if str(result) != value.lower():
        raise InvalidCommand("Invalid canonical UUID")
    return result


def _date(value: Any) -> date:
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        raise InvalidCommand("Invalid date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise InvalidCommand("Invalid date") from exc


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise InvalidCommand("Amounts must be JSON numeric values")
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise InvalidCommand("Invalid amount") from exc
    if not result.is_finite() or abs(result) >= 10 ** 16:
        raise InvalidCommand("Amount is too large")
    return result


def _normalise_lines(value: Any) -> str:
    if not isinstance(value, list) or not 2 <= len(value) <= 25:
        raise InvalidCommand("Expected 2-25 journal lines")
    # Decimal-safe exact numeric JSON encoding: never turn a financial
    # decimal into a binary float or accidentally serialize it as a string.
    prepared = []
    for line in value:
        if not isinstance(line, dict) or set(line) != {
            "account_id", "debit", "credit"
        }:
            raise InvalidCommand("Unexpected journal line shape")
        account = line["account_id"]
        if (not isinstance(account, str) or
            not 0 < len(account) <= 120 or
            any(ord(char) < 32 for char in account)):
            raise InvalidCommand("Invalid account identifier")
        debit = _decimal(line["debit"])
        credit = _decimal(line["credit"])
        if not ((debit > 0 and credit == 0) or
                (credit > 0 and debit == 0)):
            raise InvalidCommand("Journal line must have exactly one side")
        prepared.append(
            '{"account_id":' + json.dumps(account, ensure_ascii=True) +
            ',"debit":' + format(debit, 'f') +
            ',"credit":' + format(credit, 'f') + '}'
        )
    return "[" + ",".join(prepared) + "]"


def parse_finance_command(raw_bytes: bytes) -> dict:
    """Strict allowlisted data contract; actor/role/database never accepted."""
    if not isinstance(raw_bytes, bytes) or not 0 < len(raw_bytes) <= _MAX_BODY_BYTES:
        raise InvalidCommand("Invalid body size")
    try:
        parsed = json.loads(
            raw_bytes.decode("utf-8"),
            parse_float=Decimal, parse_int=int, parse_constant=_invalid_constant,
            object_pairs_hook=_unique_fields,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, InvalidOperation,
            OverflowError, ValueError, TypeError) as exc:
        raise InvalidCommand("Invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise InvalidCommand("Expected a command object")
    action = parsed.get("action")
    if not isinstance(action, str) or action not in _ALLOWED_FIELDS:
        raise InvalidCommand("Unknown finance command")
    if set(parsed) != _ALLOWED_FIELDS[action]:
        raise InvalidCommand("Unknown or missing fields")
    parsed["tenant"] = _uuid(parsed["tenant"])
    if "period_id" in parsed:
        parsed["period_id"] = _uuid(parsed["period_id"])
    if "request_id" in parsed:
        parsed["request_id"] = _uuid(parsed["request_id"])
    if "journal_id" in parsed:
        parsed["journal_id"] = _uuid(parsed["journal_id"])
    if action == "request_soft_adjustment":
        parsed["occurred_on"] = _date(parsed["occurred_on"])
        currency = parsed["currency"]
        if not isinstance(currency, str) or not _CURRENCY.fullmatch(currency):
            raise InvalidCommand("Invalid currency")
        reason = parsed["reason"]
        if (not isinstance(reason, str) or
            not 10 <= len(reason.strip()) <= 500 or
            any(ord(c) < 32 for c in reason)):
            raise InvalidCommand("Invalid adjustment reason")
        parsed["lines"] = _normalise_lines(parsed["lines"])
    return parsed


def execute_verified_finance_command(connection, *, identity, command: dict):
    """Caller MUST be a verified identity, not JSON-sourced actor/tenant grants.

    The host authenticates the bearer token first. This function only accepts
    typed commands returned by parse_finance_command and opens no dynamic SQL.
    All DB calls are parameterized, inside one rollback-safe transaction.
    """
    tenant = command["tenant"]
    if (not isinstance(identity, AuthenticatedFinanceIdentity) or
        not isinstance(identity.actor_id, UUID) or
        not isinstance(identity.allowed_tenants, frozenset) or
        tenant not in identity.allowed_tenants):
        raise PermissionError("Invalid server-verified tenant identity")
    actor = identity.actor_id
    operation = command["action"]
    with connection.transaction():
        with connection.cursor() as cur:
            # Never trust a pooled connection's or role's inherited
            # search_path. Pin PG builtins first, app schema explicitly,
            # and pg_temp last (otherwise it may shadow relation names).
            cur.execute("SET LOCAL search_path = pg_catalog, public, pg_temp")
            # PostgreSQL SET LOCAL tied to this transaction, not a retained
            # pooled session or anything received from client JSON.
            cur.execute("SELECT set_config('one.tenant_id',%s,true)",
                        (str(tenant),))
            cur.execute("SELECT set_config('one.actor_id',%s,true)",
                        (str(actor),))
            if operation in ("request_soft_close", "approve_soft_close"):
                args = (tenant, command["period_id"], actor)
            elif operation == "request_soft_adjustment":
                args = (
                    tenant, command["request_id"], command["period_id"],
                    command["journal_id"], command["occurred_on"],
                    command["currency"], command["lines"], command["reason"],
                    actor,
                )
            elif operation == "approve_soft_adjustment":
                args = (tenant, command["request_id"], actor)
            elif operation == "post_soft_adjustment":
                args = (tenant, command["journal_id"], actor)
                cur.execute(_POST_CHECK, args)
                if cur.fetchone() is None:
                    raise PermissionError("Only the approved requesting actor may post")
            else:
                raise InvalidCommand("Unsupported finance command")
            cur.execute(_SQL[operation], args)
            row = cur.fetchone()
            if row is None or not isinstance(row[0], UUID):
                raise RuntimeError("Finance command did not return an identifier")
    return {"ok": True, "id": str(row[0]), "action": operation}


class OneFinanceCommandsWSGI:
    """Separate JSON POST command endpoint; no raw SQL or actor selectors."""

    def __init__(
        self, *,
        verifier: FinanceIdentityVerifier,
        connections: TenantFinanceConnections,
        allowed_hosts: frozenset[str],
    ):
        if (not isinstance(allowed_hosts, frozenset) or not allowed_hosts or
            any(not isinstance(host, str) or not host or
                host != host.lower() or "/" in host or host.startswith(".")
                for host in allowed_hosts)):
            raise ValueError("Explicit HTTPS host allowlist required")
        self._verifier = verifier
        self._connections = connections
        self._allowed_hosts = allowed_hosts

    @staticmethod
    def _reply(start_response, status: str, message: dict | None = None,
               allow: str | None = None):
        body = json.dumps(
            message or {"ok": False, "error": "Request unavailable"},
            ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
        headers = dict(_COMMON_HEADERS)
        headers.update({
            "Content-Type": "application/json; charset=utf-8",
            "Content-Length": str(len(body)),
        })
        if allow is not None:
            headers["Allow"] = allow
        start_response(status, list(headers.items()))
        return [body]

    def __call__(self, environ, start_response):
        if environ.get("wsgi.url_scheme") != "https":
            return self._reply(start_response, "403 Forbidden")
        if environ.get("HTTP_HOST") not in self._allowed_hosts:
            return self._reply(start_response, "403 Forbidden")
        if environ.get("PATH_INFO") != _PATH:
            return self._reply(start_response, "404 Not Found")
        if environ.get("REQUEST_METHOD") != "POST":
            return self._reply(start_response, "405 Method Not Allowed",
                               allow="POST")
        if environ.get("QUERY_STRING", "") or environ.get("HTTP_COOKIE", ""):
            return self._reply(start_response, "400 Bad Request")
        if environ.get("CONTENT_TYPE") != "application/json":
            return self._reply(start_response, "415 Unsupported Media Type")
        if environ.get("HTTP_TRANSFER_ENCODING"):
            return self._reply(start_response, "400 Bad Request")
        raw_length = environ.get("CONTENT_LENGTH")
        if (not isinstance(raw_length, str) or not raw_length.isascii() or
            not raw_length.isdecimal() or len(raw_length) > 5):
            return self._reply(start_response, "400 Bad Request")
        length = int(raw_length)
        if not 0 < length <= _MAX_BODY_BYTES:
            return self._reply(start_response, "413 Payload Too Large")
        # Parse first without any DB access; unknown fields are rejected.
        try:
            stream = environ["wsgi.input"]
            raw = stream.read(length)
            if len(raw) != length:
                raise InvalidCommand("Truncated request")
            command = parse_finance_command(raw)
        except (KeyError, ValueError, AttributeError, OSError, TypeError):
            return self._reply(start_response, "400 Bad Request")
        authorization = environ.get("HTTP_AUTHORIZATION", "")
        if not isinstance(authorization, str) or len(authorization) > 8200:
            return self._reply(start_response, "401 Unauthorized")
        match = _BEARER.fullmatch(authorization)
        if match is None:
            return self._reply(start_response, "401 Unauthorized")
        try:
            # Production injector must be VerifiedRS256FinanceAccess:
            # signed issuer+audience+expiry+revocation -> local subject mapping.
            identity = self._verifier.verify_access_token(match.group(1))
            if (not isinstance(identity, AuthenticatedFinanceIdentity) or
                not isinstance(identity.actor_id, UUID) or
                not isinstance(identity.allowed_tenants, frozenset) or
                not all(isinstance(t, UUID) for t in identity.allowed_tenants) or
                command["tenant"] not in identity.allowed_tenants):
                raise PermissionError("Unauthorized actor or tenant")
        except Exception:
            return self._reply(start_response, "403 Forbidden")
        try:
            with closing(self._connections.open(command["tenant"])) as conn:
                result = execute_verified_finance_command(
                    conn, identity=identity, command=command
                )
            return self._reply(start_response, "200 OK", result)
        except PermissionError:
            return self._reply(start_response, "403 Forbidden")
        except Exception:
            # No stack trace, SQL, tokens, passwords, or database topology.
            return self._reply(start_response, "409 Conflict")
