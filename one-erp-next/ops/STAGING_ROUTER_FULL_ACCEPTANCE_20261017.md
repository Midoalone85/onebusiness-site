# ONE ERP NEXT | Private staging integration package 060 (source only)

Status: Draft review branch, 17 October 2026. **No server provisioned, no Oracle application deployed, and no production migrations run.** This package is independent of Odoo.

## What changed

### 1. Exact staging WSGI dispatcher
Source: `src/one_erp_core/staging_router.py`

The dispatcher now exposes source-code route composition for:

| Route | Method | Backend | Default |
|---|---|---|---|
| `/healthz` | GET | Precise expected deployment SHA and staging marker | Public metadata, TLS host enforced |
| `/readyz` | GET | Six independent readiness facts | HTTP 503 unless ALL verified |
| `/api/v1/operations/sales` | GET | Authorized tenant stock/order source | Disabled until ready |
| `/api/v1/operations/purchases` | GET | Authorized tenant purchase source | Disabled until ready |
| `/api/v1/operations/inventory` | GET | Authorized tenant warehouse source | Disabled until ready |
| `/api/v1/operations/returns` | GET | Authorized tenant returns source | Disabled until ready |
| `/api/v1/finance/dashboard` | GET | Existing authenticated report handler | Disabled until ready |
| `/api/v1/finance/internal-documents` | POST | Existing internal posting replay controller | **Not mounted by default** |

The server never resolves arbitrary module paths, dynamic SQL, caller-supplied database names or unauthorized tenants. It does not authorize users through `X-Forwarded-Proto`.

### 2. Three independent server-owned database registries
The constructor requires separate `operations_connections` and `finance_report_connections` registries. They must be different objects. A third `finance_write_connections` registry can only be supplied with an explicitly enabled internal financial write route AND its own trusted approved-source loader, after an independent security review. All runtime SQL roles and actual connections must be independently restricted by the operator; a separate Python registry object is necessary but does not on its own prove SQL role separation.

**Don't enable internal financial writes in the initial staging bootstrap.** Payment sources remain disabled and Saudi tax invoices are not official until the country pack and ZATCA integration are reviewed.

### 3. Actual PostgreSQL reader preflight
Source: `src/one_erp_core/staging_pg_preflight.py`

Run it on a connection established using the actual **restricted operations reader role**, not as the DBA/superuser. It checks:
- Exact physical database and SQL login name as pinned by the trusted server.
- No superuser, BYPASSRLS, CREATEDB, CREATEROLE, database CREATE or public-schema CREATE.
- Effective SELECT grants on memberships, orders, stock balances and return jobs.
- No effective INSERT, UPDATE, DELETE, TRUNCATE or TRIGGER on protected invoices, source approvals, journals and payments, including inherited and PUBLIC rights.
- Row-level security ENABLE and FORCE flags on critical finance/source tables.
- All lookups run in a READ ONLY / REPEATABLE READ transaction. Errors fail closed.

Passing only proves the checked role/catalog facts at that moment. It does NOT prove completeness of PostgreSQL cluster users, network restrictions, root credentials, tenant ownership, bank controls, statement correctness or customer isolation across separate database servers.

### 4. Four-module read-only staging smoke
Source: `src/one_erp_core/staging_smoke.py`

The operator must pass the exact allowed HTTPS staging host, 40-character deployed commit, UUID tenant and short-lived access token through environment variables, not CLI arguments.

The extended full smoke first checks `/healthz` and `/readyz`, then reads all four modules for that tenant. **No bearer token is sent until both health and readiness pass.** HTTPS certificate verification, strict host allowlist and no redirect following remain mandatory. No financial or sensitive source rows are printed.

Expected operator command (only after externally deploying and configuring a controlled staging service):

```bash
PYTHONPATH=one-erp-next/src python -m one_erp_core.staging_smoke
```

With no staging inputs it must return `NOT_RUN`. With incomplete/invalid checks, `BLOCKED`. Even a successful read-only smoke says `SMOKE_PASS_NOT_PRODUCTION_APPROVAL` and `release_approved=false`.

### 5. Separate automated evidence

- Python/PG14 integration tests: source-backed finance invariants, user permissions and restricted SQL role tests.
- Router tests: missing readiness fails before token verifier or DB, all four read routes, private report routing, no default internal commands, distinct registries, cross-tenant and host checks.
- PG catalog tests: real disposable SQL role, forbidden granted journal UPDATE, RLS checks and simulated inherited privilege mutations.
- Bilingual static-preview/private operational UI tests, finance migration/release gates and brand-identity guard.

## What is still NOT delivered

1. Live Oracle staging infrastructure and actual DNS/TLS/certificate, worker + service registration and authenticated OIDC sign-in.
2. Real inspected per-company database provisioner with vaulted per-tenant secrets, separate operations/report/write LOGIN roles, strict role grants and revocation.
3. A vetted automatic readiness probe checking **real** identity registry, tenant inventory, DB grants, migrations, isolated restore evidence and TLS. A hardcoded six-true callback would be dishonest and unsafe.
4. Real frontend sign-in + bilingual workflows bound to PostgreSQL API, with pagination, audit, logout and separate module pages. The public preview still uses fictional data.
5. Protected operator maker-checker creation/approval APIs; protected payouts, bank confirmation, billing/webhooks and retry processing; actual external status verification.
6. Saudi regulatory VAT/ZATCA issuance and review, IFRS/SOCPA financial acceptance, privacy/security penetration review, backup restore rehearsal, performance/availability testing and supported migrations.
7. Independent production release approval, monitored rollout and rollback. None follows automatically from green CI.

**Do not merge to main, run customer DB migrations, or deploy to Oracle based on this draft alone.**
