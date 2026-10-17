"""Trusted, offline JWKS rotation boundary for the ONE ERP finance API.

A backend operator must fetch JSON from the EXACT preconfigured HTTPS endpoint
using a trusted TLS client with redirects disabled and response-size limits.
This module deliberately never performs HTTP, follows discovery URLs, or trusts
a token's jku/jwk/x5u headers. A refresh is an explicit server-side operation.
No user login, token issuance, or live identity provider is configured here.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from .accounting import AccountingError
from .oidc_identity import (
    FinanceRevocationChecker,
    FinanceSubjectDirectory,
    RS256VerifierConfig,
    VerifiedRS256FinanceAccess,
)

_B64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_KID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_PRIVATE_JWK_FIELDS = frozenset({"d", "p", "q", "dp", "dq", "qi", "oth"})
_REMOTE_JWK_FIELDS = frozenset({"jku", "x5u", "crit"})
_MAX_JWKS_BYTES = 65536
_MAX_KEYS = 16


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JWKS JSON field")
        result[key] = value
    return result


def _decode_uint(raw: str) -> int:
    if not isinstance(raw, str) or not raw or len(raw) > 1500 or not _B64URL.fullmatch(raw):
        raise ValueError("Invalid RSA JWK integer")
    try:
        data = base64.urlsafe_b64decode(raw + "=" * ((-len(raw)) % 4))
    except (binascii.Error, ValueError) as error:
        raise ValueError("Invalid RSA JWK integer") from error
    if not data or data[0] == 0 or base64.urlsafe_b64encode(data).decode("ascii").rstrip("=") != raw:
        raise ValueError("Non-canonical RSA JWK integer")
    return int.from_bytes(data, "big")


def parse_rs256_jwks(document: bytes | str) -> dict[str, str]:
    """Convert only valid RS256 verification RSA public JWKs to pinned PEMs.

    Raises instead of silently accepting malformed RSA signing-key entries.
    Public data alone is not trusted: call ONLY after an approved TLS fetch.
    """
    if not isinstance(document, (bytes, str)) or len(document) > _MAX_JWKS_BYTES:
        raise ValueError("JWKS too large or unsupported")
    try:
        if isinstance(document, bytes):
            document = document.decode("utf-8", "strict")
        if len(document.encode("utf-8")) > _MAX_JWKS_BYTES:
            raise ValueError("JWKS too large")
        payload = json.loads(document, object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Invalid JWKS JSON") from error
    if not isinstance(payload, dict) or set(payload) != {"keys"}:
        raise ValueError("Unexpected JWKS document shape")
    items = payload["keys"]
    if not isinstance(items, list) or not 1 <= len(items) <= _MAX_KEYS:
        raise ValueError("Invalid JWKS key count")
    result: dict[str, str] = {}
    for jwk in items:
        if not isinstance(jwk, dict):
            raise ValueError("Invalid JWK entry")
        if jwk.get("kty") != "RSA" or jwk.get("alg") not in (None, "RS256"):
            continue
        if jwk.get("use") not in (None, "sig"):
            continue
        if set(jwk).intersection(_PRIVATE_JWK_FIELDS | _REMOTE_JWK_FIELDS):
            raise ValueError("Private material or remote key URL forbidden")
        if "key_ops" in jwk and jwk["key_ops"] != ["verify"]:
            raise ValueError("Unexpected JWK key operation")
        kid = jwk.get("kid")
        if not isinstance(kid, str) or not _KID.fullmatch(kid) or kid in result:
            raise ValueError("Missing or duplicate RSA key ID")
        n, e = _decode_uint(jwk.get("n")), _decode_uint(jwk.get("e"))
        if e != 65537 or not 2048 <= n.bit_length() <= 8192 or n % 2 != 1:
            raise ValueError("RSA JWK key does not meet policy")
        try:
            public_key = rsa.RSAPublicNumbers(e=e, n=n).public_key()
            pem = public_key.public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            ).decode("ascii")
        except ValueError as error:
            raise ValueError("Invalid RSA public key") from error
        result[kid] = pem
    if not result:
        raise ValueError("No permitted RS256 signing keys")
    return result


class TrustedJWKSStore:
    """Time-bounded trusted key snapshot, replaced atomically on refresh.

    The source_url comparison is a provenance guard only. The CALLER is
    responsible for fetching the exact configured URL over validated TLS,
    rejecting redirects, using DNS/egress policy, and bounding network reads.
    """

    def __init__(self, *, jwks_url: str, max_age_seconds: int = 1800):
        if not isinstance(jwks_url, str):
            raise ValueError("Configured JWKS URL required")
        parsed = urlsplit(jwks_url)
        if (parsed.scheme != "https" or not parsed.hostname or
                parsed.username is not None or parsed.password is not None or
                parsed.query or parsed.fragment or not parsed.path.startswith("/") or
                parsed.port not in (None, 443) or
                not 60 <= max_age_seconds <= 3600):
            raise ValueError("Explicit HTTPS JWKS URL and bounded TTL required")
        self.jwks_url = jwks_url
        self.max_age_seconds = max_age_seconds
        self._keys: Mapping[str, str] = MappingProxyType({})
        self._expires_at: datetime | None = None

    def refresh_from_document(self, document: bytes | str, *,
                              source_url: str, fetched_at: datetime | None = None) -> None:
        if source_url != self.jwks_url:
            raise ValueError("JWKS source URL mismatch")
        when = fetched_at if fetched_at is not None else datetime.now(timezone.utc)
        if not isinstance(when, datetime) or when.tzinfo is None or when.utcoffset() is None:
            raise ValueError("Timezone-aware refresh time required")
        now = datetime.now(timezone.utc)
        if abs((when - now).total_seconds()) > 60:
            raise ValueError("JWKS refresh clock too far from current time")
        keys = parse_rs256_jwks(document)
        # A failing refresh NEVER extends the lifetime of old trusted keys.
        self._keys = MappingProxyType(keys)
        self._expires_at = when + timedelta(seconds=self.max_age_seconds)

    def valid_keys(self, *, now: datetime | None = None) -> Mapping[str, str]:
        moment = now if now is not None else datetime.now(timezone.utc)
        if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("Timezone-aware verification time required")
        if self._expires_at is None or moment >= self._expires_at or not self._keys:
            raise AccountingError("Trusted signing keys unavailable or stale")
        return self._keys


class JWKSBackedFinanceAccess:
    """Local JWT verifier using only fresh, preapproved signing key snapshots."""

    def __init__(self, *, issuer: str, audience: str, keys: TrustedJWKSStore,
                 directory: FinanceSubjectDirectory,
                 revocations: FinanceRevocationChecker):
        if not isinstance(keys, TrustedJWKSStore):
            raise ValueError("Trusted signing key store required")
        self._issuer = issuer
        self._audience = audience
        self._keys = keys
        self._directory = directory
        self._revocations = revocations

    def verify_access_token(self, raw_token: str):
        config = RS256VerifierConfig(
            issuer=self._issuer, audience=self._audience,
            signing_keys_by_id=self._keys.valid_keys(),
        )
        return VerifiedRS256FinanceAccess(
            config, self._directory, self._revocations
        ).verify_access_token(raw_token)
