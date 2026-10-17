# ONE ERP NEXT | Independent invoice reviewer evidence (062)

**17 October 2026, development-only, NO ORACLE/PRODUCTION DEPLOYMENT**

## New complete source-review path

1. Maker creates a version-frozen **internal** commercial sale intent and invoice source from actual dispatched/valued/unbilled order lines (059/060).
2. Independent reviewer signs in to a private app under a trusted OIDC provider, chooses an exact tenant + invoice UUID.
3. Optional, disabled-by-default staging GET \`/api/v1/finance/internal-invoice-source?tenant=<uuid>&document_id=<uuid>\` verifies HTTPS exact host, JWT signature via provided verifier, company entitlements, independently instantiated **restricted** review reader role, PostgreSQL non-superuser/non-BYPASSRLS status, effective tenant/actor membership and dynamic \`finance.invoice_source.review\` permission. It only reads unapproved **commercial** invoices from immutable source, matching sale intent, open period, line allocation, approved internal tax-policy reference and currency precision. A creator may not review their own source.
4. The server reads and recalculates every line as exact Decimal at the configured rounding precision: ordered source line number, item description, quantity, price, discount, internal tax category/rate/code, net/tax/gross. Returns all lines (1–100) and 11 summary fields. It never relies on browser amounts or browser tax rates.
5. UI displays genuine source details and line table using \`textContent\`, then requires a distinct acknowledgement. Click issues only \`{tenant,document_id}\` to a **separate** POST backend. The POST and PostgreSQL approval trigger recheck current membership, distinct maker, duplicate-approval unique key and 062 dynamic permission **at approval time**.
6. Readiness must be actually satisfied (IdP, tenant routing, restricted roles, migrated database, tested restore, live TLS) before either route can be opted in. By default **both routes are unmounted**. Neither is a ZATCA invoice, bank payment or legal tax assessment.

## Security review and explicit deployment gate

- **Do not expose** private UI on GitHub Pages or public fictional-data preview. No bearer tokens in URL, cookies, localStorage, code, logs or source.
- Operator must provision **distinct SQL logins** for operations reader, finance report reader, invoice source review reader and finance approval writer. Container registry inequality alone does not establish DB grants.
- Review reader MUST be nonprivileged with RLS active. Grant only SELECT for required tenant-scoped tables, currency precision, memberships, global permission template/approval reads and EXECUTE on \`one_actor_has_permission\`; never grant SQL editor, role management, SET ROLE, CREATE or mutation. Independently audit the actual effective privileges.
- Fresh source GET is a point-in-time immutable snapshot. Approval POST must revalidate member/permission/unique row server-side and reject stale, already approved, cancelled or otherwise incompatible state. Route remains disabled until staging threat review.
- Country tax policy shown is **internal/unverified**, not government approval. Production requires country pack, ZATCA UBL2.1/QR/CSID/signature/SDK/sandbox, clearance/reporting lifecycle and a regulated original archive.
- No customer money movement, bank match, invoice legal issuance, SaaS billing, main merge or staging/prod deploy is included here.

## Acceptance and limitations

- Unit tests for trusted source calculations, rate/line/number mismatch, creator self-review, denied role/permission, invalid input and signed GET tenant/host/method restrictions.
- Node UI tests validate strict JSON evidence, exact references, line table, explicit acknowledgement, stale/cross-tenant rejection, no cookies, no cache.
- Staging router tests verify default denial, readiness gates and physically separate reader registry precondition.
- CI PostgreSQL 14 migration regression exercises permission-trigger integration from the existing full source-ledger suite. **No real staging DBA deployment acceptance or end-to-end browser authentication is represented by CI.**
