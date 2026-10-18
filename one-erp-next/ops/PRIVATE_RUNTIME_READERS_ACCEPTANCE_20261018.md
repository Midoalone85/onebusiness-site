# ONE ERP NEXT | Read-only PostgreSQL runtime roles: acceptance gate

**18 October 2026 | source and disposable CI tests only | NO Oracle deployment or customer grants**

## The security problem this work addresses

Earlier role templates are **not proof of effective least privilege**:
- `report_reader_privileges.sql` covers the initial financial dashboard, but not all of the later receivables/invoice-review/activity sources.
- `operations_read_role_grants_review_056.sql` is a role-grant proposal, not proof that a real SQL login inherited only those privileges.
- `REVOKE ... FROM named_role` does not revoke access from `PUBLIC` or another membership; PostgreSQL grants `EXECUTE` to `PUBLIC` on newly created functions by default unless carefully configured.
- PostgreSQL RLS session variables (`one.tenant_id` / `one.actor_id`) are **not authentication**. User-supplied SQL, an overprivileged login, or a second database connection bypassing the trusted factory invalidates this boundary.

**New code:** `src/one_erp_core/runtime_reader_privilege_audit.py`. This independently evaluates **effective** login and inherited-group rights from PostgreSQL catalogs in a read-only transaction, including `PUBLIC`, `NOINHERIT` + `SET ROLE` memberships, tables, views, sequences, functions, other user schemas, forced RLS and database/temp/schema privileges. An unreviewed privilege or omitted required permission is a release blocker.

**New tests:** `tests/test_runtime_reader_privilege_audit_postgres.py` provisions a **randomly named, disposable CI database and SQL login** and deliberately grants/revokes unsafe rights. It tests failure on PUBLIC function/table grants, direct mutation/sequence rights, hidden role membership, lost FORCE RLS, unapproved schema/table/view/function, TEMP/CREATE rights and mismatched deployment policies. All fixtures refuse to run on an unspecified/customer DB.


**Additional PostgreSQL column ACL protection:** the inspector checks `has_any_column_privilege` as well as `has_table_privilege`. A column-only `SELECT` on an unapproved table, a partial read on an approved table, or any column-only `INSERT`, `UPDATE` or `REFERENCES` is a release blocker. This is tested on PostgreSQL, not inferred from SQL templates.

**New composition:** `src/one_erp_core/staging_tenant_db_acceptance.py` exposes `verify_staging_finance_reader` for a *separately approved operator process*, consuming **two distinct connections**: a privileged catalog inspector and the named restricted financial reporting login, both pinned to the same tenant database. It requires all three independently implemented verifications to pass in order: `verify_exclusive_tenant_inventory`, `verify_physical_tenant_database`, and `inspect_restricted_reader`. A forged or contradictory result fails shut. The output explicitly sets `route_ready=False` and `release_approved=False`. It never enables the public /readyz endpoint or issues customer access on its own.

The companion `tests/test_staging_tenant_db_acceptance.py` adds fail-closed composition tests. Existing real-PG suites independently validate the component SQL behavior and cross-tenant isolation; the composition tests are mocking the orchestration, not proof of a live Oracle tenant.


## Proposed reviewer scopes: starting checklist, NOT approved production role policy

Every independently privileged route family must have a *separately verified runtime SQL login*. The finance dashboard and receivables currently share the same reviewed finance-report connection registry; the audit must allow only the union of their approved reads. The same login must never be reused between operational reads, privileged financial writes, reviewer approvals, invoice-review evidence and activity reads without an explicitly reviewed source-code change.

| Protected route/consumer | Required source families to reconcile with source code |
| --- | --- |
| Operations (sales/purchases/inventory/returns) | `one_memberships`, permission templates/change requests/approvals, commercial orders and lines, products, warehouses, stock balances, return jobs |
| Finance reporting + receivables | Dashboard's chart/journal/member facts **plus** `one_invoice_docs`, `one_payment_docs`, `one_payment_allocations`, and permission facts; cash applied internally is **not externally bank verified** |
| Invoice-review evidence | `one_invoice_source_requests`, `one_sales_invoice_source_links`, `one_sales_invoice_intents`, `one_commercial_orders`, `one_periods`, `one_invoice_source_approvals`, permission/membership facts, and reviewed `one_currency_precision` global lookup |
| Per-document activity timeline | `one_commercial_orders`, `one_commercial_change_audit`, `one_invoice_audit`, `one_invoice_source_requests`, `one_invoice_source_approvals`, `one_payment_audit`, `one_ledger_audit`, `one_return_audit`, `one_stock_events`, `one_fulfillment_audit`, `one_receiving_audit`, permission/membership facts |

`one_currency_precision` is a possible **explicitly reviewed global reference table**; never turn off RLS for an unreviewed tenant-specific table to satisfy an audit.

The required allowed `public.one_actor_has_permission(uuid,uuid,text)` function must be reviewed as an exact signature; remove default PUBLIC EXECUTE for unrelated application functions without breaking reviewed maintenance routines. Do **not** apply global REVOKE/ALTER DEFAULT PRIVILEGES on Oracle/production without an independent migration and role-impact review.

## Before enabling a real reader

1. Verify **physical dedicated database** for the named tenant and a complete negative peer-role connection inventory using the separate `tenant_inventory` and `tenant_readiness` gates. The inspected login must not own customer tables, carry DB/schema DDL privileges or expose arbitrary SQL to a browser.
2. A separate elevated operator obtains approval/MFA and a short-lived secret-managed *inspector connection*, not the app/reader connection. Pin the database and concrete runtime login from control-plane records; do not accept user-supplied DB names/role policies.
3. Freeze and **review** a `RestrictedReaderPolicy` for that exact deployed service login, with an exact required/allowed table list, explicitly exempted global lookups, exact function signatures, and exact allowed membership roles. All other grants must fail closed. This does **not** automatically create/grant/revoke roles.
4. Invoke `inspect_restricted_reader(inspector_connection,expected_database=...,policy=...)` and capture only the sanitized operator-side evidence. Failure blocks staging readiness, not just a dashboard badge. Verify independently that role and SQL login are correctly bound and cannot inherit an unreviewed role.
5. Validate RLS policy **predicates** under a restricted login with two real, disposable tenants; the catalog inspector confirms `FORCE RLS` exists but cannot prove its predicate is correct. Verify company membership revocation and attempt cross-tenant reads using a second connection and malicious request.
6. Rehearse whole-tenant backup/restore, HTTPS route ownership, provider-backed OIDC/MFA, actual token/permissions expiry, and staged rollback. Check that PostgreSQL has no unexpected future default privileges, alternate schemas/owners or unsafe SECURITY DEFINER paths.
7. Require independent security/finance/DBA review and a human-signed release approval. Re-run if any new migration or role/default privilege changes. Treat CI success as *development evidence only*.

## Architecture decision still applies

ONE ERP NEXT remains **one logical modular core**, no seven unrelated cores. The backend must keep sessions, authorization, idempotent invoice/payment effects, audit and tenant-routing safety compatible with running multiple application processes in the future. Do not create paid Oracle replicas or enable autoscaling now.

## Hard boundaries / known limits

- No Oracle tenant/login, hosted IdP, customer login, real physical tenant provisioning, restored backup, TLS or production SQL role was inspected.
- This audit cannot attest to user identity, physical firewall/pg_hba, cross-database SQL FDWs, malicious SQL execution, SQL SECURITY DEFINER logic, or complete SQL policy correctness.
- It currently inspects the PostgreSQL catalog as of a single snapshot, **not future GRANTs or upgrades**. An automated, independently operator-owned staging integration and new cross-tenant abuse tests are needed.
- Approval/payment write roles require a **separate least-privilege audit**, not reuse of the read-only checker.
- No merge, production migration, invoice issuance, statutory ZATCA clearance, bank settlement, or external billing was performed.
