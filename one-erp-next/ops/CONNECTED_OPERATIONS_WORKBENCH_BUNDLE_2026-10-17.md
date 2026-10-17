# ONE ERP NEXT | Connected Operations Workbench bundle

Date: 2026-10-17.
Branch: feature/one-erp-smart-finance-20261015, draft PR #20.
This is an independent development integration, not the older hosted
application, not a replacement for production and not live Oracle deployment.

## What the bundle actually adds

A cohesive private read-only commercial operations slice. It is
**not** the fictional-data GitHub Pages prototype.

| Source | Purpose |
| --- | --- |
| \`src/one_erp_core/operations_read_model.py\` | Five tenant- and membership-scoped PostgreSQL read models with fixed SQL, repeatable-read read-only snapshot, bound query params and pagination (25 x <=101 pages). |
| \`src/one_erp_core/operations_wsgi.py\` | Five strict HTTPS/Bearer JSON endpoints. Verifier checks server-owned actor/tenant grants **before** opening a tenant connection. |
| \`src/one_erp_core/operations_portal_wsgi.py\` | Single HTTPS host dispatching private JSON endpoints and a static no-data web shell with strict CSP. |
| \`ops/operations_reader_privileges.sql\` | Dedicated NOLOGIN SELECT-only role for actual operations SQL tables, no posting/issuing privileges. |
| \`web/operations.html\` | Responsive FIVE-module private workspace shell with RTL/LTR and separate module locations. |
| \`web/operations.css\` | Compact blue design for desktop/mobile; no third-party assets. |
| \`web/operations.js\` | Browser navigator / real data pagination, safe DOM text rendering, English/Arabic switching, client-only ephemeral bearer use. |
| \`tests/test_operations_wsgi.py\` | Strict TLS, host/path/method/query/token/tenant validation and leakage controls. |
| \`tests/test_operations_portal_wsgi.py\` | Exact static route whitelist, CSP/no-store, API handoff and no hard-coded data. |
| \`tests/test_postgres_integration.py\` | Added actual PostgreSQL reader role tests, sales/purchase/stock/refund records and actual HTTP-to-PostgreSQL role integration test. |

## Actual private endpoints

- \`GET /app/sales\`
- \`GET /app/purchases\`
- \`GET /app/inventory\`
- \`GET /app/returns\`
- \`GET /app/refunds\`
- \`GET /assets/operations.js\`
- \`GET /assets/operations.css\`
- \`GET /api/v1/operations/{sales|purchases|inventory|returns|refunds}?tenant=<verified-uuid>&page=0\`

The GET shell/asset pages are public because they contain ZERO private data.
JSON data routes require HTTPS with exact reverse-proxy configured scheme
(not trusting spoofable \`X-Forwarded-Proto\`), exact allowlisted \`Host\`,
a short-lived JWT supplied solely in \`Authorization: Bearer\`, an injected
verifier that has independently signed/verified claims and resolved active
company entitlements server-side, tenant-specific connection factory, a
nonprivileged read-only database role, second DB membership check, and
PostgreSQL row-level security.

The query parameters never include credentials, actor IDs, database DSNs or
arbitrary SQL. Exact five SQL queries are fixed in source, allowlist
arguments only, paginate to avoid unbounded response size and serialize
Decimal currency as strings. Data passes into browser tables exclusively
through \`textContent\` to avoid HTML injection. No sample financial numbers
or fake customer rows are returned. Browser keeps bearer token only in page
memory and clears it on reload/disconnect; it cannot recover login sessions.
The UI marks refund bank references as UNVERIFIED.

## Example WSGI assembly (pseudo-operational dependency injection)

Trusted hosting (NOT users) must supply:

\`\`\`python
from one_erp_core.operations_portal_wsgi import OneOperationsPortalWSGI
from one_erp_core.finance_wsgi import TenantFinanceConnections

app = OneOperationsPortalWSGI(
    verifier=trusted_oidc_verifier,  # independent trusted issuer, keys and revocation
    connections=TenantFinanceConnections({
        approved_tenant_uuid: callable_opening_exact_tenant_read_only_db
    }),
    allowed_hosts=frozenset({"operations.company.example"}),
)
\`\`\`

Serve only behind a trusted HTTPS reverse proxy/WAF, signed identity
provider, and deployment-specific production secrets. The code does NOT
provision identity, supply actual tenant DSNs, install a bank feed or configure
Oracle hosting. All bearer token input is for independent developer testing.
The browser shell is not a production login screen.

## Financial integrity signal on inventory rows

The live inventory row includes `quantity_reconciled`, which is TRUE
only when a matching stored valuation quantity exists and equals the
physical on-hand balance for that warehouse/product. Missing valuation
does not silently become "reconciled". This is a lightweight source
indicator, **not** a complete historical inventory-to-GL reconciliation,
IAS 2 cost audit, physical cycle count, or stock-period signoff.

## Test acceptance

1. Test runner builds all SQL migrations 001–044 in disposable PostgreSQL 14.
2. Install scoped operations reader role; verify it cannot INSERT commercial
   products or execute period close or journal-posting.
3. Verify real tenant/source rows from sales/purchases/stock/refunds,
   exact output schemas, 25-row page bounds and nonprivileged DB reads.
4. Verify one browser HTTP JSON request routes across token verifier,
   tenant authorization, read role and PostgreSQL, and a cross-company
   request is denied BEFORE opening the DB.
5. Verify strict origin/HTTPS/body/query/path controls, generic backend
   errors, connection closure, no-store / CSP, safe text output and no
   exposure of token in URLs or HTML.
6. Confirm failed historical tests caused by fixed Oct 15-16 dates were
   repaired using the *execution date* only for strict same-day NRV and
   bank-approval fixtures, without relaxing production source checks.

## Still NOT done (explicit release blockers)

- Backend ID provider OIDC account login/MFA/password reset/recovery,
  real production signing keys, authoritative revocation, sessions, and
  full support for on-device mobile sign-in.
- Persistent per-customer databases and verified isolated Oracle staging,
  least-privilege production runtime grants, migration/backup/restore and
  tested TLS/load/monitoring.
- Commercial WRITE APIs for purchasing, quotations, receiving, delivery,
  invoice/returns, finance journals and approvals. They must execute
  validated atomic source-backed DB operations with safe idempotency,
  maker/checker authorization and full posted source reconciliation.
- Customer/supplier/contact masters, units/variants/serial/lot costing,
  multi-warehouse transfers, complex discounts and mixed-currency tax flows.
- KSA ZATCA signed official XML credit/invoice rules and reporting/clearance,
  invoice sequence/hash/QR, VAT country pack, actual bank provider
  statement and settlement proof, IFRS/SOCPA company-specific policies.
- End-to-end browser automation with real hosted identity and tenancy,
  production security review, company provisioning, commercial subscription
  checkout/rebills and real sector packs (healthcare/manufacturing, etc.).

**Release state:** Code on isolated feature branch; no production
data changed, no money moved. The visible original
https://onebusiness.site/one-erp-preview/ is still a MOCK DATA demo and
is NOT this connected workbench.

The operations shell is a real-data, server-connectable *developer preview*
which can only show a tenant's information once legitimate OAuth/OIDC access
and a real tenant DB are supplied by the trusted hosting layer.
Do not claim commercial readiness or a live URL on this evidence.
