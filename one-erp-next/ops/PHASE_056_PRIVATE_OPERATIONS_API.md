# ONE ERP NEXT / 056 | Connected-data operations API handoff

Date: 2026-10-17. **Source branch:** \`feature/one-erp-next-live-operations-read-056-20261017\`, created from the **verified** 055 commercial permission branch.

This is a **larger development package**, not a deployed Oracle service. It does
not change or replace the existing static website, legacy server or the
blue Arabic/English UX already chosen. It contains no Odoo modules, assets or
imports. The public UI remains an unconnected fictional-data preview.

## Actual code in this package

- \`db/056_scoped_operations_read_permissions.sql\`: four independently revocable
  per-user/per-role rights:
  \`sales.order.view\`, \`purchase.order.view\`,
  \`inventory.stock.view\`, \`inventory.return.view\`.
  Existing 048 policy proposals and independent approval are respected.
  Read permission is NOT permission to confirm a sale, dispatch, reserve,
  return, write a journal or issue a payment.
- \`operations_read_repository.py\`: four static SQL reads; each is one
  PostgreSQL **REPEATABLE READ / READ ONLY** transaction, must have a
  non-superuser/non-BYPASSRLS connection, verified tenant/actor context, a
  live membership and the exact relevant operation view permission. Tenant
  RLS is defense in depth, not primary authentication.
- \`operations_read_wsgi.py\`: HTTPS-only WSGI JSON GET boundary with
  pinned host allowlist, exact paths, Bearer access token verified by a
  server-injected trusted authenticator, company selection only from
  server-confirmed tenant entitlements and strictly pinned DB factories.
  No arbitrary SQL, caller connection strings, UI-assigned actor IDs,
  external CORS grants, mutation methods or public caching.
- \`tests/test_operations_read_wsgi.py\`: endpoint validation, serialization,
  read-only method rules, authentication, tenant denial before DB opening,
  no cross-tenant fallback, no error/secret disclosure and DB cleanup.
- Additional PostgreSQL integration tests in
  \`tests/test_postgres_integration.py\` exercise real source-backed order and
  stock views, per-user grant revocation, viewer versus administrator access,
  deterministic pagination, actor isolation and refusal of privileged DB role.

## JSON routes (backend only)

- \`GET /api/v1/operations/sales?tenant=<uuid>&limit=25&offset=0\`
- \`GET /api/v1/operations/purchases?tenant=<uuid>&limit=25&offset=0\`
- \`GET /api/v1/operations/inventory?tenant=<uuid>&limit=25&offset=0\`
- \`GET /api/v1/operations/returns?tenant=<uuid>&limit=25&offset=0\`

Only the \`Authorization: Bearer <server-verifiable-JWT>\` header is accepted.
The tenant UUID is a **request for an authorized tenant**, never a grant.
Pagination caps: 1–50 rows per page and offset no greater than 5000.
All quantities and unit prices are decimal strings (e.g. \`"12.500"\`).
The schema contains \`items\`, \`module\`, \`tenant_id\`, \`page\` with
\`has_more\`/\`next_offset\`, \`read_only:true\` and source identifier.
Nothing here can mutate financial or inventory data.

Example **shape only** (NOT a live company record):

\`\`\`json
{
  "module": "inventory",
  "tenant_id": "11111111-1111-1111-1111-111111111111",
  "items": [
    {
      "warehouse_id": "22222222-2222-2222-2222-222222222222",
      "warehouse_code": "WH01",
      "warehouse_type": "sellable",
      "product_id": "33333333-3333-3333-3333-333333333333",
      "sku": "DEMO-A",
      "product_name": "Sample",
      "on_hand": "7.000",
      "reserved": "2.000",
      "available": "5.000"
    }
  ],
  "page": {"limit":25,"offset":0,"has_more":false,"next_offset":null},
  "read_only": true,
  "source": "one_erp_next_postgresql"
}
\`\`\`

## Bank, IFRS and release status

This does **NOT** supply bank ingestion/payouts, audited year end, production
IFRS/SOCPA or ZATCA statutory notes, verified OIDC sign-in, customer data in
the static UI, tenant creation in Oracle, payroll/HR, POS, hospital or factory
functions. It **does** make a concrete, testable data boundary available for
subsequent integration with live independent module pages.

## Production gates (not implemented by merely merging this PR)

1. Wire a real trusted IdP, verify issuer/audience/signature/expiry/revocation,
   MFA policy and signed identity-to-tenant mapping on every request.
2. Supply only database connections to the **one physically isolated tenant
   database** chosen by the verified server, never a shared DB fallback.
3. Provision and audit a minimal operational-reader PostgreSQL role with
   read-only table grants + reviewed execution of
   \`one_actor_has_permission\`, and prevent arbitrary SQL and GUC access from
   any browser account. Disposable CI runtime is **too privileged**.
4. Review TLS terminator host configuration, gateway CSRF/CORS/rate limits,
   safe logs, availability, access audit, incident response and restore.
5. Connect the chosen RTL Arabic/LTR English module pages and ONE PULSE to
   these private endpoints, implement a full integration test against the
   staging backend with browser token validation, not invented dashboard data.
6. Write-operation endpoints (sales confirmation, costing, dispatch, receipts,
   refunds) must use separately scoped user-permission-aware transactional
   procedures and an independently reviewed command boundary. GET-only 056
   must **never** be treated as authority to create financial documents.

## Regression notes

The existing 030 real QA reassessment SQL deliberately requires a fresh
assessment date equal to PostgreSQL \`current_date\`. Older CI fixtures had
hardcoded 2026-10-15 and became invalid on 2026-10-17; the test fixture now
uses the current date **without relaxing the database safety rule**.
A separate historical activity test now uses a future date window to avoid
assuming CI will always run on its original authoring day.

Do not merge or deploy this draft until current CI passes both Python 3.11 and
3.13 under PostgreSQL 14, plus production security approval.
