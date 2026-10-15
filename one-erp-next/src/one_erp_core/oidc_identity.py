"""Offline RS256 verification for trusted ONE ERP bearer access tokens.

No login UX or token issuance is implemented here. Production requires an
external identity provider issuing short-lived access tokens for this exact
audience, a secure source of current signing public keys, and an authoritative
server-side subject-to-company membership directory.

Public keys must be loaded from trusted configuration or an allowlisted issuer
JWKS updater outside this request path. NEVER let JWT header URLs choose keys.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID

import jwt

from .accounting import AccountingError
from .finance_http_boundary import AuthenticatedFinanceIdentity


class FinanceSubjectDirectory(Protocol):
    def resolve(self, issuer: str, subject: str) -> AuthenticatedFinanceIdentity:
        """Retrieve local actor UUID and authorized tenant IDs from server records."""
        ...


class FinanceRevocationChecker(Protocol):
    def is_revoked(self, issuer: str, jwt_id: str) -> bool:
        """Return True for revoked sessions. Raise if authoritative source fails."""
        ...


@dataclass(frozen=True, slots=True)
class RS256VerifierConfig:
    issuer: str
    audience: str
    signing_keys_by_id: Mapping[str, str]
    max_token_lifetime_seconds: int = 900
    leeway_seconds: int = 10

    def __post_init__(self) -> None:
        if (not self.issuer.startswith("https://") or
            not self.audience or not self.signing_keys_by_id or
            self.max_token_lifetime_seconds < 60 or
            self.max_token_lifetime_seconds > 3600 or
            not 0 <= self.leeway_seconds <= 60):
            raise ValueError("OIDC verification configuration is incomplete or invalid")
        if any(not key_id or not isinstance(key_pem, str) or
               "BEGIN PUBLIC KEY" not in key_pem
               for key_id, key_pem in self.signing_keys_by_id.items()):
            raise ValueError("Only configured PEM RSA public keys with IDs are accepted")


class VerifiedRS256FinanceAccess:
    """Verifies an issuer-bound access token and resolves local entitlements.

    Never accepts tenant authorizations directly from arbitrary request data or
    the token's unverified header. A verified token's subject maps to internal
    actor and memberships via an authoritative server directory.
    """

    def __init__(self, config: RS256VerifierConfig,
                 directory: FinanceSubjectDirectory,
                 revocations: FinanceRevocationChecker) -> None:
        self.config = config
        self.directory = directory
        self.revocations = revocations

    def verify_access_token(self, raw_token: str) -> AuthenticatedFinanceIdentity:
        if not isinstance(raw_token, str) or not raw_token or len(raw_token) > 8192:
            raise AccountingError("Authentication failed")
        if raw_token.count(".") != 2:
            raise AccountingError("Authentication failed")
        try:
            # Header is not trusted. Only use kid to select a key in our
            # trusted allowlist. Never use header-provided jwk, jku or x5u.
            header = jwt.get_unverified_header(raw_token)
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
                raise AccountingError("Authentication failed")
            if any(name in header for name in ("jku", "jwk", "x5u", "crit")):
                raise AccountingError("Authentication failed")
            pem = self.config.signing_keys_by_id.get(header["kid"])
            if pem is None:
                raise AccountingError("Authentication failed")
            claims = jwt.decode(
                raw_token,
                key=pem,
                algorithms=["RS256"],
                issuer=self.config.issuer,
                audience=self.config.audience,
                leeway=self.config.leeway_seconds,
                options={
                    "require": ["iss", "aud", "sub", "iat", "nbf", "exp", "jti"],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_iat": True,
                    "verify_nbf": True,
                    "verify_iss": True,
                    "verify_aud": True,
                    "strict_aud": True,
                },
            )
            now = datetime.now(timezone.utc).timestamp()
            issued_at = claims["iat"]
            expires_at = claims["exp"]
            not_before = claims["nbf"]
            if (type(issued_at) is not int or type(expires_at) is not int or
                type(not_before) is not int or
                expires_at <= issued_at or
                expires_at - issued_at > self.config.max_token_lifetime_seconds or
                issued_at > now + self.config.leeway_seconds or
                not_before > expires_at):
                raise AccountingError("Authentication failed")
            if claims.get("token_use") != "access":
                raise AccountingError("Authentication failed")
            subject = claims.get("sub")
            jti = claims.get("jti")
            if (not isinstance(subject,str) or not 0 < len(subject) <= 255 or
                not isinstance(jti,str) or not 0 < len(jti) <= 255):
                raise AccountingError("Authentication failed")
            if self.revocations.is_revoked(self.config.issuer, jti):
                raise AccountingError("Authentication failed")
            identity = self.directory.resolve(self.config.issuer, subject)
        except (jwt.exceptions.PyJWTError, ValueError, TypeError, KeyError,
                AccountingError) as error:
            raise AccountingError("Authentication failed") from error
        except Exception as error:
            # Directory/revocation service failure must not default to allow.
            raise AccountingError("Authentication temporarily unavailable") from error
        if (not isinstance(identity, AuthenticatedFinanceIdentity) or
            not isinstance(identity.actor_id, UUID) or
            not isinstance(identity.allowed_tenants, frozenset) or
            not all(isinstance(tenant, UUID) for tenant in identity.allowed_tenants)):
            raise AccountingError("Authentication failed")
        return identity
