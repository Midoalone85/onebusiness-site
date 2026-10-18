# ONE ERP NEXT | Oracle staging operator handoff

**18 Oct 2026** | Status: **NOT DEPLOYED / STAGING NOT VERIFIED**
**Scope:** Secure operator-only acceptance plan, not a service installation, invoice issuance, customer migration or production approval.

## Architecture / cost boundary

The system is an **independent ONE ERP NEXT application with one logical modular core** and a dedicated-database-per-company goal. Deploy **one controlled application deployment** for the first commercial candidate. Do not pay for horizontal replicas, cloud autoscaling, additional DB clusters, new public services, or long-running diagnostic instances as part of this handoff.

The user-selected target is Oracle Cloud Saudi Arabia West (Jeddah), but no Oracle Console instance, SSH shell, OCI API, DNS certificate, running application process, PostgreSQL production role, customer sign-in, backup or restored copy was touched or verified by this development-only change.

## Gate 0: freeze an actual source candidate

- Pin and review the exact Git SHA for **all** relevant branches. PR #53 is a *draft* targeting PR #52's feature branch, not `main`; do not click merge just because CI passes.
- Run `one-erp-next-integration/rehearse_main_candidate.py` through the read-only GitHub candidate audit. It protects the original public site plus both invoice/receivables sibling sources, but it is not a SQL permission or production security audit.
- Confirm full Python 3.11/3.13, PostgreSQL 14/16/17 where supported, Javascript private UI, source preservation and operational checks pass on the **same reviewed SHA**. Verify no skipped acceptance suite and no error misreported as test success.
- Capture a reviewed rollback SHA and database migration compatibility plan.

## Gate 1: independently controlled Oracle staging (no customer migration)

- Obtain explicit **operator authorization** before any OCI resource change. Inspect the chosen tenancy/compartment/region and reconcile expected resources and current costs.
- Establish a dedicated non-production host with trusted public DNS/HTTPS; hostname and certificate must be verified from outside the instance. Never treat a temporary sslip.io URL or unverified nginx file as a valid certificate.
- Keep service-account, OIDC verifier, privileged inspector and app reader secrets outside source control, process arguments, issue comments, URLs and client bundles. Staging tokens must be stored with a real secrets manager.
- Pin the reviewed application artifact digest and actual deployed Git SHA. Health is liveness only; do not inject six hardcoded true checks into `StagingReadiness`.

## Gate 2: per-company DB and effective rights (requires real Oracle staging)

Perform **operator-controlled, read-only** audits with two different connections to the **same exact dedicated tenant DB**:

1. Privileged catalog inspector, approved independently and never made accessible to the public WSGI app.
2. Actual restricted finance-report SQL login, with current active actor+company membership.

Use `one_erp_core.staging_tenant_db_acceptance.verify_staging_finance_reader` with pinned company UUID, actor UUID, DB name, SQL login, complete forbidden-peer reader-role inventory and an **independently DBA-reviewed** `RestrictedReaderPolicy`. This composes, in order:
- `verify_exclusive_tenant_inventory`: exactly one tenant, correct database, deny PUBLIC CONNECT and peer-role CONNECT.
- `verify_physical_tenant_database`: real restricted login, forced RLS, exact expected table set, no invoice/payment table SELECT, active company membership.
- `inspect_restricted_reader`: all effective SQL ACLs including PUBLIC, direct and inherited grants, NOINHERIT/SET ROLE membership, column-only privileges, user schemas, views, routines, sequences and DDL/TEMP.

**Only a three-pass result counts as `STAGING_DB_PREFLIGHT_ONLY`; it always returns `route_ready=False` and `release_approved=False`.** Inspectors must verify real policy predicates, ordinary user revocation, and network/firewall/pg_hba settings separately.

Never install finance/report SQL privileges into a customer DB just to make a test green. Review `ops/PRIVATE_RUNTIME_READERS_ACCEPTANCE_20261018.md` and all default EXECUTE grants. Reader and writer privileges must remain separate.

## Gate 3: restore rehearsal, cryptographic evidence and actual application

- Take a real encrypted backup of a disposable tenant and **restore it to a separate isolated target**, with a documented recovery result, checksums, retention and rollback test. Merely enabling backup does not count.
- Verify OIDC issuer, audience, expiry, MFA, revocation and membership updates on live staging, without bypassing the real IdP.
- Run authenticated read-only staging smoke **only after** exact SHA, host, TLS, tenant and readiness have been independently checked. `staging_smoke.py` has a dedicated five-variable opt-in and never sends its JWT before matching the reviewed host and readiness.
- Rehearse denial cases: wrong company, revoked user, missing/expired token, missing DB, wrong SQL reader, forbidden POST, duplicate invoice posting, interrupted commit, and rejected maker=checker.
- Have finance and security owners approve realistic journal/stock/AR reconciliation and migration rollback.

## Gate 4: separate commercial launch blockers (not solved by this work)

- Real customer sign-up/provisioning and user recovery.
- Payments/subscription webhook verification, issuance of commercial invoices and trial-to-paid transitions.
- Saudi tax/ZATCA statutory integration and final IFRS/SOCPA finance acceptance.
- Audit retention and operations/on-call, alerting, regulatory privacy, backup rotation, app/browser E2E, load testing and independent security review.

## Destructive CI policy

Some integration suites apply all SQL migrations and create/drop ephemeral DBs or users. As of this review, their `setUpClass` entrypoints require **`CI=true`, `dbname=one_erp_test`, loopback host, port 5432, and no `hostaddr` override before obtaining an admin connection**. Run those suites on GitHub's disposable PostgreSQL service, **never** through a connection copied from Oracle. The review of all future test scripts remains an operator responsibility.

## Decision

**NO GO on Oracle deployment or commercial release until real operator-provided evidence for gates 1-4 is collected and signed.** This handoff does not request access credentials or cause OCI changes. The user may continue developing and testing safely on the existing feature branch while commercial prerequisites are addressed.
