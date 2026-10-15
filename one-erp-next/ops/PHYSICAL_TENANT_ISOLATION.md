# ONE ERP Next: physical customer database isolation

**Status: validated only on disposable GitHub Actions PostgreSQL 14.**
This is NOT an Oracle deployment or a tenant sign-up service. Do not execute
CI test fixture setup or teardown against production.

## What the automated proof actually does

`tests/test_physical_tenant_isolation.py` creates two separately named
PostgreSQL databases on the temporary CI cluster, each with:

- Its own database name, tenant UUID and independently generated SQL login.
- All **four existing finance SQL migrations** (ledger, close/reversal,
  invoice, payment) applied to that database alone.
- `REVOKE ALL ON DATABASE ... FROM PUBLIC`; only its matching SQL login
  receives `CONNECT`.
- A backend read-only login with `SELECT` on exactly the five
  finance snapshot tables and no table/schema write grants. It cannot read
  invoice or payment subledger tables.
- An entirely independent sample company row and membership, solely for CI.
- Explicit negative tests: account A cannot connect to database B, account B
  cannot connect to A, neither can mutate tables/create objects, changing
  self-set RLS values does not import another database's customer records.

The test owns the two random database and role names and drops those exact
resources afterward. Its admin DSN comes only from the dedicated CI
environment. No password or customer credential is committed.

This is one important infrastructure test, **not** a production security
certification. The current finance domain schema still uses tenant_id and
RLS for defense in depth; the dedicated database is an additional physical
isolation boundary.

## Proposed production provisioning workflow (NOT YET IMPLEMENTED)

1. A verified organization administrator signs in through a real IdP with
   enforced MFA; a trusted backend checks explicit provisioning permission,
   approver identity, request idempotency and subscription entitlements.
2. The control plane reserves the tenant ID and a unique server-generated
   DB/role identifier, recording the approved operation and its state in an
   externally retained audit log. Do not mark tenant ready here.
3. Infrastructure creates a **separate database and its own restricted
   credentials**, with secrets from a vault. Never send secrets in process
   arguments, source control, URLs, user-facing responses or logs.
4. Apply only reviewed, version-pinned migrations using a privileged
   migration connection. Never grant migration-table ownership, superuser,
   CREATE or BYPASSRLS to an application/report connection.
5. Install app and reporting grants; restrict PostgreSQL network and
   connection origins; explicitly deny PUBLIC access to customer databases.
6. Seed tenant identity inside its own database. Verify exactly one active
   tenant binding, currency/country configuration and approved initial CoA.
7. Test cross-database `CONNECT` refusal, read/write grants, access-token
   tenant checks, signed issuer lookup and disabled tenant behavior.
8. Produce a real, encrypted and integrity-verified backup in independent
   storage, then perform an isolated restore drill and recovery-time check.
9. Only after all checks pass, atomically mark the control-plane route
   **READY** and expose it via validated HTTPS / tested DNS. On error, mark
   provisioning failed, revoke connection credentials, quarantine or
   dispose of safely-created resources using audited cleanup. Never
   silently select a shared/default DSN.
10. Test subscription billing, renewal, ZATCA regulatory workflows and
    full finance transaction lifecycle before opening real customer access.

## Explicit remaining blockers

- Real operator authentication, trusted MFA/approval workflow and
  provisioning orchestration with durable idempotency.
- Vault-backed DB credential issuance/rotation; tenant-aware backend
  connection pools and private network/pg_hba enforcement.
- Atomic coordination between SQL cluster DDL and control-plane state,
  orphan detection, retries, resource quotas and provider billing cleanup.
- Production-grade append-only audit export, encrypted backup creation,
  **actual** restore drills, availability/recovery exercises and alerts.
- Penetration testing, zero-trust network review, real identity-provider
  lifecycle, ZATCA compliance and production financial correctness review.

## CI

Pipeline: `.github/workflows/one-erp-next-finance-tests.yml`.
Use the latest successful commit SHA on the **draft finance feature branch**,
not an earlier green check, before claiming this proof passed.

Do not merge or deploy this feature branch automatically.
