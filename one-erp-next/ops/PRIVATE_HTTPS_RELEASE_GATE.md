# ONE ERP Finance HTTPS Route: release gate (non-production)

This folder contains a **standalone WSGI application component**, not a running
website. The financial report will work only when integrated by a trusted
Python application with its own authentication/connection provisioning.

## Implemented contract

- GET `/api/v1/finance/dashboard`, query exactly `tenant` (UUID),
  `from` (YYYY-MM-DD), `as_of` (YYYY-MM-DD), `currency` (ISO code),
  and optional `locale=ar|en`. The period is bounded to 367 inclusive days.
- `Authorization: Bearer <JWT>` is required. Credentials in query strings
  or cookies are not accepted by this route.
- WSGI server must attest `wsgi.url_scheme=https`; `HTTP_HOST` must match
  an explicit allowlist. A client-supplied `X-Forwarded-Proto` is ignored.
- JWT authentication resolves actor and active company entitlements from
  trusted server-side control-plane identity; unauthorized tenant access is
  rejected **before** retrieving its connection.
- `TenantFinanceConnections` takes an explicit map of tenant UUID to
  secret-managed PostgreSQL connection factory. It has NO shared fallback.
- The report snapshot revalidates active DB membership and builds an
  authorized report via read-only, repeatable-read PostgreSQL.
- HTML responses deny framing, disable caching and scripts, and escape
  account and company data. Failure responses never expose SQL, private
  tokens, tenant UUIDs or internal connection addresses.

## Still required before any deployment

1. Register and verify a real identity provider's discovery and signing
   keys using a secure trust bootstrap. Rotate trusted public keys; don't
   fetch signing keys from token-supplied URLs. Configure audience, issuer,
   revocation source, token TTL and clock synchronization.
2. Provision a control-plane database **separate** from finance tenant DBs.
   Implement audited user/company onboarding, entitlement removal, token
   revocation writes, incident recovery and MFA.
3. Create **one PostgreSQL database per company** with audited migrations,
   encrypted backup, and restricted read-only connection roles. Configure
   a distinct per-tenant DSN in a secure server-side registry. Fail closed
   for unknown/deactivated tenants. CI intentionally shares a disposable
   database and does NOT prove physical multi-database isolation.
4. Run a trusted WSGI server behind TLS-terminating proxy. Ensure only the
   proxy/server sets `wsgi.url_scheme` and the host; force TLS, configure
   HSTS, apply rate limits/timeouts, request size ceilings, safe security
   logs, access control, monitoring and alerting. **Never** print JWT or
   `Authorization` headers.
5. Connect the site sign-in UX and customer-specific authorized routes;
   conduct full browser, logout, expiry, revocation, load and pen tests.
6. Resolve accounting country rules, FX, VAT/ZATCA, privacy, retention,
   disaster-recovery and invoice-integrity checks before accepting customers.

This module and its mocked/CI tests are **not** a production deployment.
Never publish financial HTML on static GitHub Pages.
