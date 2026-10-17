# ONE ERP NEXT | Private activity timeline, phase 1

**17 October 2026 | Source code and non-production CI, NOT deployed**

## Product behavior

A compact activity sidebar appears to the **left of the invoice reviewer** on wide layouts (including RTL Arabic) and **below the document** on phones. It contains audit-backed action, authenticated actor UUID and timestamp, formatted in the company host's supplied IANA timezone (review workspace currently defaults to Asia/Riyadh). RTL Arabic / LTR English labels are supported; no fake history is shown.

The first wired record page is the internal invoice review. Its activity panel loads **only after** that document source has been fetched and independently validated. The reusable `mountActivityTimeline(...)` can be mounted by other trusted per-record screens without cloning the security code. It never writes to the audit or treats client clicks as committed business events.

## Actual audit facts supported in the repository

| Record type | Existing PostgreSQL source | Audited events |
| --- | --- | --- |
| invoice | `one_invoice_audit`, `one_invoice_source_requests`, `one_invoice_source_approvals` | issued, requested for review, independently approved |
| payment | `one_payment_audit` | applied internally (NOT bank confirmed) |
| journal | `one_ledger_audit` | posted |
| return | `one_return_audit` | requested / completed / rejected |
| stock | `one_stock_events` | stock movement recorded |
| delivery | `one_fulfillment_audit` | stage transitions |
| receiving | `one_receiving_audit` | inspection stages |

All seven types use fixed allowlisted parameterized SQL. Read starts a REPEATABLE READ, READ ONLY transaction, checks non-superuser/non-BYPASSRLS database role, sets tenant/actor GUCs on the **trusted** connection, and verifies `one_actor_has_permission(...,'audit.activity.view')` and active membership. Records are scoped by tenant and exact UUID; only minimal actor/action/time facts are exposed. The existing RLS policies stay in force. No payloads, amounts, passwords, patient data or raw change JSON are returned.

## New files and integration

- Migration `db/063_private_activity_read_permissions.sql`: user-scoped view permission, admin/accountant allowed by default; viewer denied by default, overrides follow reviewed 048 policy
- Service `activity_repository.py`: bounded permission-checked PostgreSQL reader
- `activity_wsgi.py`: signed GET `/api/v1/activity/timeline?tenant=<uuid>&record_type=invoice&record_id=<uuid>&limit=20&offset=0`
- `staging_router.py`: `enable_activity_read=False` default; activation needs an independently restricted `activity_connections` registry and a passing **real** readiness probe
- Private UI: `activity-client.mjs`, `activity-timeline.mjs`, and responsive styles, with stale-request abort, exact source verification and no browser local storage

## Boundaries / obligations before real use

1. **Not an audit of every action yet.** Draft document edits, every field old/new value, permission changes, unsuccessful login attempts, every report view, and every button click require separate server-side audit producer coverage. Do NOT log secrets, health data, or sensitive plaintext. Business mutation auditing should commit atomically in the **same transaction** as the successful mutation; failed attempts belong in a separate secure security telemetry stream. Never emit fake UI-generated “committed” events.
2. Actor UUID is real and stable but **person name** is not available from these tenant DB tables. Host should resolve it through a trusted identity directory only after access checks, with carefully bounded disclosure; never infer names from browser claims.
3. No raw SQL or browser-selected DB role. A DBA must independently grant exactly necessary SELECT on the seven existing audit sources, invoice request/approval tables, 048 permission tables and related membership data to a dedicated low-privilege activity reader. Review effective PUBLIC and inherited privileges and RLS; no grants in this source migration.
4. OIDC login/MFA, one-database-per-tenant Oracle staging, HTTPS cert, ready checks, actual authenticated API gateway, backup restore, migration acceptance and privilege testing remain pending. Do not enable `enable_activity_read` or publish the sensitive panel on GitHub Pages before staging signoff.
5. On logout/tenant switch, the authenticated private shell MUST dispose of the workspace and revoke its session at the IdP. UI only clears in-memory history; it cannot revoke tokens.
6. Later phases: extend actor/action/time and before/after-safe summaries across commercial order create/update, inventory, accounting adjustments, approvals, expenses, HR, configuration, and superuser/security events. Configure retention and tamper-evidence under approved legal/accounting policy.

**CI success is not production acceptance; the new endpoint remains off by default.**
