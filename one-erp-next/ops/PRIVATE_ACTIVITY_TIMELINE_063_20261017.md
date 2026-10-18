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

## Activity refresh after committed invoice approval (source-only continuation)

The private invoice reviewer now accepts an optional synchronous `onApproved` hook. Only a successful, matching server-side `approved_internal_source` response invokes the hook. The workspace checks its active tenant, page generation, and validated document reference, then **re-reads** the PostgreSQL-backed activity API. The browser never inserts a claimed approval event. Failed/denied/stale replies do not notify. The activity panel also offers a manual Refresh history button for legitimate updates made by another authenticated actor; permission failures clear visible history.

This is **not live deployment**, push updates, identity-directory attribution, or an every-action audit. Existing source-only, blocked-by-default staging constraints and independent verification requirements remain unchanged.

## Sales and purchase order confirmation timeline (development continuation)

The activity reader now supports `sales_order` and `purchase_order` as **confirmation-only** views from `one_commercial_orders.confirmed_by` and `confirmed_at`. These immutable-after-confirmation source fields are not a full order edit trail. It does not claim who created or edited a draft, because migration 005 does not store a verified actor/timestamp for those edits. Fixed SQL requires exact tenant/order IDs and sale vs purchase kind plus confirmed status. The audit reader requires **both** `audit.activity.view` and the appropriate `sales.order.view` / `purchase.order.view` actor permission in one read-only snapshot before fetching any order. Dedicated DBA-reviewed activity role also needs minimal tenant-RLS-restricted SELECT on `one_commercial_orders` before safe staging enablement.

In the private sales and purchases table, a per-order **سجل العمليات / Activity history** button is shown only for a canonical source-backed `order_id`. It reads the authenticated activity API only after user click, and clears/disposes the prior history on module switch, denied API or logout. It never invents confirmation from a status string, and no public preview/Oracle deployment is changed. These are 9 supported record types total; only invoice, sales and purchase order are currently wired directly to interactive private screens.

## Phase 2: transactional commercial draft events (064, source-only)

Migration `064_commercial_draft_change_audit.sql` adds a new tenant-scoped, append-only `one_commercial_change_audit` table and source AFTER triggers. Restricted actors with active tenant membership and a matching server-provided actor/tenant context produce atomic committed events for order draft creation, line add/edit/removal. The immutable change audit records only line number, product/warehouse UUIDs and numeric ordered quantity and price **before/after** values. It excludes partner names, passwords, descriptions, health data and arbitrary user input. Unchanged line updates produce no event; confirmed-line fulfillment continues to use existing stock audit. The draft audit does not yet claim approval of draft edits or log browser clicks.

The existing nine-type private per-record activity API now merges the `created/line_added/line_changed/line_removed` source facts with confirmed orders. It returns only event ID, actor UUID, action and timestamp; it **does not expose stored financial before/after JSON** to the general timeline. A separately reviewed, narrowly scoped details permission and private read route are still required for field-diff display. `audit.activity.view` and the matching sales/purchase view permission remain mandatory. The dedicated activity reader needs an independently reviewed minimal SELECT on the new audit table, not a blanket privilege grant.

Special case: disposable CI fixture setup uses privileged migration-owner connections, which skip the new audit trigger. Actual application database logins must be non-superuser/non-BYPASSRLS and isolated from migration accounts, so this fixture exception is not a live application authorization path. PostgreSQL session GUCs **cannot authenticate themselves**; real OIDC/MFA identity, least-privilege SQL grants, RLS, external audit review, rollback and Oracle staging tests must precede activation. No production migration or customer event has been recorded.

## Separate permission for financial before/after details (065)

Migration `065_order_change_detail_permission.sql` adds `audit.order_changes.view` with company administrator default allow and accountant/viewer default deny, subject to the independently approved 048 permission workflow. The signed `GET /api/v1/activity/timeline` has optional `include_changes=1` **only** for sales/purchase orders. The backend verifies active tenant membership, `audit.activity.view`, module read right and `audit.order_changes.view` in one read-only transaction. It returns safe allowlisted immutable source diffs, never browser-supplied values. The UI shows a deliberate **عرض التغييرات / Show changes** action and does not fetch confidential prices automatically. A denied detail read clears the panel; no details may be inferred from the basic event list.

Activation requires audited SQL SELECT grants for the dedicated nonprivileged activity reader, provider-verified identity/session, DBA reviewed RLS across tenants and explicit staging signoff. No public demo, Oracle deployment or production data access is authorized.
