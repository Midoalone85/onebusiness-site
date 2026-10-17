# ONE ERP NEXT | Read-only staging smoke, not a deployment

Status: review-only source on 17 October 2026; not connected to Oracle or a live staging route.

## What is implemented

- A fail-closed staging HTTP client in src/one_erp_core/staging_smoke.py.
- A GET-only WSGI health/readiness contract in src/one_erp_core/staging_health.py.
- Offline adverse tests for unsafe hosts, redirects, wrong SHA, wrong tenant, fake rows, revoked/missing access, cacheable data, network errors and secret hygiene.
- Health reports only the service name, staging environment and exact code SHA.
- Readiness is **false by default** and becomes true only when an operator-owned probe supplies all six exact boolean facts: trusted IdP; isolated tenant database routing; restricted SQL roles; approved migrations; independently restored backup; live HTTPS certificate.
- Finance/sales protected data is never printed. The smoke performs HTTPS GET health and readiness first, and sends the bearer token **only after** the exact expected staging SHA and readiness have passed. It never sends a bearer token to the health endpoints or to a mismatched/unready host. Only then may it do one authorized sales read for the named company.

## Required deployment work BEFORE a smoke can run

1. Deploy a separate non-production ONE ERP NEXT WSGI service (do not connect it to legacy Odoo or the public static preview) behind a trusted TLS endpoint and strict exact host allowlist.
2. Wire GET /healthz and GET /readyz from staging_health.py into that WSGI server. Deployed SHA must come from the reviewed CI artifact, not a browser query.
3. Provide an independently implemented, checked readiness callback using actual IdP configuration, physical-tenant database inventory, restricted login / PUBLIC grant review, exact migrations, a completed isolated backup-restore rehearsal and a validated live TLS endpoint. Do not just return six True literals.
4. Wire authenticated GET /api/v1/operations/sales to a trusted OIDC verifier, tenant registry and least-privilege reader role; the response must be private and no-store. Do not show demo rows as customer data.
5. From an operator-controlled machine, set the **five environment variables** ONE_STAGING_ORIGIN, ONE_STAGING_ALLOWED_HOST, ONE_STAGING_COMMIT_SHA, ONE_STAGING_TENANT_ID and ONE_STAGING_ACCESS_TOKEN. Pass the token only via local secret management, never as a command argument, copied terminal transcript, CI artifact or GitHub text.
6. Run with PYTHONPATH=one-erp-next/src and module invocation python -m one_erp_core.staging_smoke. With incomplete configuration it returns NOT_RUN exit status 2, not a simulated success. With a failed check it returns BLOCKED exit 1. It emits only check booleans, scope and release_approved=false.

## Important control and evidence limits

- HTTPS and certificate verification are performed by Python against the explicitly allowed public staging host on port 443. IP literals, loopback, local/test domains and wildcard-address nip.io/sslip.io are rejected. No redirects or proxy fallback are followed; no customer URLs are derived from user data.
- A returned readiness boolean is an operational smoke input, **not** proof of actual PostgreSQL role restrictions, isolated company database ownership, backup integrity, KSA tax compliance, network firewall security or third-party certification. Independent evidence must be collected by reviewers.
- A passing result deliberately says SMOKE_PASS_NOT_PRODUCTION_APPROVAL and always release_approved=false.
- The source changes do not provision or start Oracle instances, add an IdP, ship tax e-invoices, settle bank payments or create customer accounts.
- Remain on draft branches and run no customer migrations, actual payment capture or main merge until separate written release acceptance.

## Remaining release gates

Real Oracle-hosted staging, verifiable DNS/TLS, runtime service account and secrets, authenticated account and company onboarding, backup/restore drill, browser E2E of sales/purchases/stock/returns/invoice/payment, load/penetration tests, Saudi VAT/ZATCA integration, provider billing and human security/finance signoff.
