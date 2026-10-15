# ONE ERP Next: staged tenant onboarding safety gates (draft)

This is the **control-plane journal and read-only database preflight**, not a
working customer sign-up service or Oracle deployment. GitHub Actions uses
disposable PostgreSQL 14 and synthetic checkpoints, which are NOT equivalent
to a successful infrastructure rollout or backup restore.

## Stage sequence

A new tenant registry row defaults to **inactive**, and
`one_auth.register_tenant` no longer enables it automatically.

`one_auth.begin_tenant_bootstrap` records an approved, server-supplied job and
event ID. Every stage is serialized by PostgreSQL row locks and transitions
exactly once in this fixed sequence:

1. `requested` (tenant inactive)
2. `database_created` (tenant inactive)
3. `schema_migrated` (tenant inactive)
4. `isolation_verified` (tenant inactive)
5. `restore_verified` (tenant inactive)
6. `route_verified` (tenant inactive)
7. `ready` (tenant can become active)

Skipping a stage, replaying an event with different inputs, using the wrong
operator ID or a non-SHA256-sized evidence digest all fail closed. Exact
retries with the same job and event ID return the existing state without
creating a second audit event. `fail_tenant_bootstrap` moves a pending
job to `failed`, keeps the tenant inactive, and records only a sanitized
failure code. Failed jobs cannot silently resume or be overwritten by an
unrelated onboarding request.

`one_auth.set_tenant_active(...,true,...)` now deliberately refuses manual
reactivation via the old function. Disabled tenants need a new, independently
authorized reactivation protocol, not a direct active=true toggle.

## What the checkpoints can and cannot prove

The SQL functions enforce ordered **records** and role boundaries. A 32-byte
digest is NOT proof that any checkpoint happened, because a dishonest or
buggy orchestrator could submit arbitrary 32 bytes. They MUST NOT be exposed
through a public HTTP API or handed to tenants, browsers or tenant DB roles.

- The trusted, authenticated backend must verify a real admin approval,
  independent infrastructure results, migration checksums, cross-database
  access denial, successful encrypted backup AND isolated RESTORE drill,
  working HTTPS DNS/cert/routing, and documented release authorization.
- Only after those independent checks does the trusted operator submit
  a SHA-256 digest of approved immutable evidence into the transition log.
  Never record keys, tokens, database passwords, DSNs or patient information.
- `tenant_readiness.verify_physical_tenant_database` performs a **limited,
  read-only** pinned-db and restricted-role check, using a read-only
  repeatable-read transaction. It checks the expected database and SQL role,
  no elevated PostgreSQL role/schema privileges, eight required tables and
  forced RLS, visible tenant identity and active membership.
- This test cannot show whether another tenant exists but is hidden by RLS.
  The infrastructure admin must independently inventory the database,
  verify one tenant only, and prove cross-database CONNECT access is denied.
- A valid DB readiness fingerprint covers **only the database identity
  checkpoint**. It does not certify the backup, restore, DNS, HTTPS, identity
  provider, gateway billing, ZATCA, availability or security posture.

## Operational safety policy

- Keep control plane separate from dedicated per-customer finance DBs.
- SQL provisioning roles are backend-only. `operator_id`, approvals,
  job IDs and event IDs must come from the vetted server context, never
  from unauthenticated web fields.
- Do not run any of the CI database creation/drop fixtures on Oracle.
- `002_audited_provisioning.sql` precedes
  `003_tenant_bootstrap_gate.sql`. Migration 003 intentionally changes the
  behavior of registered-but-not-ready companies. Existing live tenants
  require a reviewed, reversible migration plan and contract compatibility
  checks before applying it.
- Do not run production migrations, enable trial-to-paid auto billing,
  merge the draft PR, or expose login routes without independent reviews,
  real encrypted recovery drills, secured credentials and a tested rollback.

CI runs the 003 state machine in a disposable shared control-plane
database and physical database readiness preflight against separately
created disposable finance databases. **All infrastructure stages in
the state-machine tests use explicitly synthetic CI digests**.
