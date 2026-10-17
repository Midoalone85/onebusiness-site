"""Read-only, fail-closed ONE ERP NEXT *staging* HTTP smoke.

This module never posts finance commands, approves sources, migrates a DB,
checks a bank, or issues an official tax invoice. A passing smoke is NOT
production release authorization, TLS pen testing, or proof of DB isolation.
Nothing runs without a named external staging host, tenant, commit and bearer
token supplied to the operator's local process/environment.
"""
from __future__ import annotations

from dataclasses import dataclass
import http.client
import ipaddress
import json
import os
import re
import ssl
from typing import Callable
from urllib.parse import urlencode, urlsplit
from uuid import UUID

_SHA = re.compile(r"[0-9a-f]{40}\Z")
_HOST = re.compile(r"(?=.{4,253}\Z)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\Z")
_LIMIT = 8192


class InvalidStagingConfiguration(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class StagingSmokeConfig:
    origin: str
    allowed_host: str
    commit_sha: str
    tenant_id: UUID
    access_token: str
    timeout_seconds: float = 5.0

    def __post_init__(self):
        if (not isinstance(self.origin, str) or
            not isinstance(self.allowed_host, str) or
            not isinstance(self.commit_sha, str) or
            not isinstance(self.tenant_id, UUID) or
            not isinstance(self.access_token, str) or
            not isinstance(self.timeout_seconds, (int, float)) or
            isinstance(self.timeout_seconds, bool) or
            not 0.2 <= self.timeout_seconds <= 10):
            raise InvalidStagingConfiguration("Invalid staging smoke configuration")
        u = urlsplit(self.origin)
        host = u.hostname
        if (u.scheme != "https" or u.username or u.password or
            u.port not in (None, 443) or u.query or u.fragment or
            u.path not in ("", "/") or not host or
            u.hostname != self.allowed_host or
            self.allowed_host != self.allowed_host.lower() or
            not _HOST.fullmatch(self.allowed_host) or
            self.allowed_host.endswith((".local", ".internal", ".localhost", ".test", ".nip.io", ".sslip.io")) or
            self.allowed_host in ("localhost", "example.com")):
            raise InvalidStagingConfiguration("Public exact HTTPS staging host required")
        try:
            ipaddress.ip_address(self.allowed_host)
        except ValueError:
            pass
        else:
            raise InvalidStagingConfiguration("IP literal staging targets forbidden")
        if (not _SHA.fullmatch(self.commit_sha) or
            not _TOKEN.fullmatch(self.access_token) or
            len(self.access_token) > 8192):
            raise InvalidStagingConfiguration("Expected SHA and JWT token required")


def _network_get(host: str, path: str, headers: dict[str, str],
                 timeout: float) -> tuple[int, dict[str, str], bytes]:
    """Only direct HTTPS/443 with OS-trusted certificate verification.

    No cross-origin redirect following or ambient proxy configuration. Caller
    has already verified host equals the explicit trusted staging allowlist.
    """
    conn = http.client.HTTPSConnection(
        host, 443, timeout=timeout, context=ssl.create_default_context())
    try:
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        result = response.read(_LIMIT + 1)
        return response.status, {
            key.lower(): value for key, value in response.getheaders()
        }, result
    finally:
        conn.close()


def _read_json(*, transport, host, path, headers, timeout):
    code, received_headers, body = transport(host, path, headers, timeout)
    if (code != 200 or len(body) > _LIMIT or
        not received_headers.get("content-type", "").lower().startswith(
            "application/json") or
        "no-store" not in received_headers.get("cache-control", "").lower()):
        return None
    try:
        item = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeError):
        return None
    return item if isinstance(item, dict) else None


def run_staging_smoke(
    config: StagingSmokeConfig, *,
    transport: Callable = _network_get,
) -> dict:
    """Probe exact staging routes without leaking responses or token contents.

    Does not infer database/grant/security properties from readiness self-report.
    Any exception or malformed shape yields a generic failure without secrets.
    """
    if not isinstance(config, StagingSmokeConfig):
        raise InvalidStagingConfiguration("Typed staging config required")
    common = {"Accept": "application/json", "User-Agent": "one-erp-next-smoke"}
    host = config.allowed_host
    results = {"health_sha": False, "ready": False, "sales_read": False}
    paths = [
        ("health_sha", "/healthz", common),
        ("ready", "/readyz", common),
        ("sales_read", "/api/v1/operations/sales?" + urlencode({
            "tenant": str(config.tenant_id), "limit": 1, "offset": 0,
        }), {
            **common, "Authorization": "Bearer " + config.access_token,
        }),
    ]
    for name, path, headers in paths:
        # Never release the bearer token to a host that has not first
        # attested the exact expected staging SHA and a ready backend.
        if name == "sales_read" and not (results["health_sha"] and results["ready"]):
            break
        try:
            item = _read_json(
                transport=transport, host=host, path=path, headers=headers,
                timeout=config.timeout_seconds)
            if name == "health_sha":
                results[name] = bool(
                    item and item.get("service") == "one-erp-next" and
                    item.get("environment") == "staging" and
                    item.get("commit_sha") == config.commit_sha)
            elif name == "ready":
                # This checks *only* advertised readiness. Independent
                # actual database isolation/security checks remain mandatory.
                results[name] = bool(item and item.get("ready") is True)
            else:
                results[name] = bool(
                    item and item.get("module") == "sales" and
                    item.get("tenant_id") == str(config.tenant_id) and
                    item.get("read_only") is True and
                    item.get("source") == "one_erp_next_postgresql" and
                    isinstance(item.get("items"), list))
        except Exception:
            results[name] = False
    return {
        "status": "SMOKE_PASS_NOT_PRODUCTION_APPROVAL" if all(results.values())
        else "BLOCKED",
        "checks": results,
        "scope": "read_only_staging_http_only",
        "release_approved": False,
    }



def run_full_staging_smoke(
    config: StagingSmokeConfig, *, transport: Callable = _network_get,
) -> dict:
    """Full 4-module private read acceptance; NO mutation and NO released status.

    Preserve the baseline three probes and their token non-disclosure property,
    then sample independently authorized source-backed reads for purchases,
    inventory and returns. This is a contract smoke, not proof the underlying
    tenant business data, SQL roles or ZATCA are production-ready.
    """
    baseline = run_staging_smoke(config, transport=transport)
    checks = dict(baseline["checks"])
    for module in ("purchases", "inventory", "returns"):
        checks[module + "_read"] = False
    if baseline["status"] != "SMOKE_PASS_NOT_PRODUCTION_APPROVAL":
        return {
            "status": "BLOCKED", "checks": checks,
            "scope": "read_only_staging_four_modules",
            "release_approved": False,
        }
    headers = {
        "Accept": "application/json", "User-Agent": "one-erp-next-smoke",
        "Authorization": "Bearer " + config.access_token,
    }
    for module in ("purchases", "inventory", "returns"):
        path = "/api/v1/operations/" + module + "?" + urlencode({
            "tenant": str(config.tenant_id), "limit": 1, "offset": 0,
        })
        try:
            item = _read_json(
                transport=transport, host=config.allowed_host, path=path,
                headers=headers, timeout=config.timeout_seconds)
            checks[module + "_read"] = bool(
                item and item.get("module") == module and
                item.get("tenant_id") == str(config.tenant_id) and
                item.get("source") == "one_erp_next_postgresql" and
                item.get("read_only") is True and
                isinstance(item.get("items"), list) and
                isinstance(item.get("page"), dict) and
                item["page"].get("limit") == 1 and
                item["page"].get("offset") == 0
            )
        except Exception:
            checks[module + "_read"] = False
    return {
        "status": "SMOKE_PASS_NOT_PRODUCTION_APPROVAL" if all(checks.values())
        else "BLOCKED",
        "checks": checks,
        "scope": "read_only_staging_four_modules",
        "release_approved": False,
    }


def main() -> int:
    """Explicit opt-in; secrets stay in environment and never in output."""
    required = (
        "ONE_STAGING_ORIGIN", "ONE_STAGING_ALLOWED_HOST",
        "ONE_STAGING_COMMIT_SHA", "ONE_STAGING_TENANT_ID",
        "ONE_STAGING_ACCESS_TOKEN",
    )
    if any(not os.environ.get(name) for name in required):
        print(json.dumps({
            "status": "NOT_RUN", "release_approved": False,
            "reason": "Trusted staging host, SHA, tenant or access token not configured",
        }))
        return 2
    try:
        config = StagingSmokeConfig(
            origin=os.environ["ONE_STAGING_ORIGIN"],
            allowed_host=os.environ["ONE_STAGING_ALLOWED_HOST"],
            commit_sha=os.environ["ONE_STAGING_COMMIT_SHA"],
            tenant_id=UUID(os.environ["ONE_STAGING_TENANT_ID"]),
            access_token=os.environ["ONE_STAGING_ACCESS_TOKEN"],
        )
        report = run_full_staging_smoke(config)
    except Exception:
        print(json.dumps({
            "status": "BLOCKED", "release_approved": False,
            "reason": "Staging preflight rejected configuration or probe",
        }))
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "SMOKE_PASS_NOT_PRODUCTION_APPROVAL" else 1


if __name__ == "__main__":
    raise SystemExit(main())
