# ONE ERP NEXT | Private integrated finance workspace

**17 October 2026 | DRAFT development source only | NOT A DEPLOYMENT**

## What is now connected in the combined source branch

This branch is stacked on reviewed-but-unmerged accounting feature branches. It brings prior independent source-review and receivables-aging prototypes into ONE combined development tree, preserving both SQL migrations, safety tests and staging router routes.

| Workspace section | Private client route | Capability and restrictions |
| --- | --- | --- |
| Sales | \`GET /api/v1/operations/sales\` | Confirmed company-only source-backed rows; no order writes |
| Purchases | \`GET /api/v1/operations/purchases\` | Actual order lines; no supplier payment writes |
| Inventory | \`GET /api/v1/operations/inventory\` | Source on-hand/reserved fields; no movement writes |
| Returns | \`GET /api/v1/operations/returns\` | Return states and lineage; no refund/credit issue |
| Receivables | \`GET /api/v1/operations/receivables\` | As-of aging and exact decimal values; **internal allocations, NOT bank-confirmed receipts** |
| Invoice review | \`GET /api/v1/finance/internal-invoice-source\` (explicit opt-in only) | Actual immutable sales invoice lines, internal tax decision and maker/checker authorization; no statutory ZATCA invoice |

A SEPARATE, disabled-by-default approval POST exists for independent reviewers. Its UI requires an explicit acknowledgment; it sends only tenant and invoice UUID. The database independently enforces membership, reviewer different from creator, and 062 dynamically overridable per-user permission at approval time.

## Corrected original UI/backend contract drift

Earlier private operations screens requested \`/api/v1/operations/<module>\` without tenant and displayed \`rows\`. The REAL backend demands \`tenant\`, accepts \`limit/offset\`, and returns \`items\` with \`page\`, \`module\`, \`tenant_id\`, \`read_only\` and \`source\`. The new client now sends a canonical tenant, insists on matching exact company/module metadata, validates basic field types and refuses cross-tenant or fictional source responses. The page now renders actual \`items\` only. Denied or stale company data is wiped.

The bilingual shell \`private-ui/finance-workspace.mjs\` creates compact separate pages and one main navigation for Sales, Purchases, Stock, Returns, Aging and Invoice Review; changing modules disposes the prior page. The source-only CSS \`private-ui/finance-workspace.css\` is blue, compact, responsive and RTL/LTR aware. It uses text-only DOM for all backend content, never innerHTML.

## Important system integration contract

A trusted external application host MUST supply a verified tenant UUID, exact HTTPS API base URL, short-lived in-memory OIDC access token provider, and explicit as-of date. Neither the JavaScript shell nor this repository manufactures a customer login, OAuth access token, bank transaction, tax clearance, actual revenue figure or tenant entitlement.

Example (a skeleton for a **trusted private** host only):

\`\`\`js
import {mountPrivateFinanceWorkspace} from "./finance-workspace.mjs";
const workspace=mountPrivateFinanceWorkspace({
  root:document.getElementById("private-finance"),
  baseUrl:"https://approved-private-api.example.test/",
  tenant:serverAuthorizedCompanyId,
  as_of:"2026-10-17",
  getToken:()=>verifiedInMemorySession.getAccessToken(),
  language:"ar",
});
await workspace.select("sales");
// On verified host logout, ALSO revoke/end session server-side, then workspace.dispose()
\`\`\`

The local UI reset button is deliberately named **Clear displayed data**, not "Log out", because reset does not invalidate the backend session.

## Hard blockers before deployment

1. All branches stacked over draft PRs 44–53 require independent source review and a controlled, conflict-free merge chain. A successful CI run is NOT a production release.
2. Deploy a real Oracle-hosted isolated staging service with owned DNS/TLS, trusted reverse proxy, OIDC IdP + MFA, secret-managed connection registry, independently restricted SQL login roles and verified 061/062 migrations.
3. Execute real one-database-per-tenant physical isolation testing and signed company entitlements. Verify effective PostgreSQL GRANTs and RLS with production-like users; code registries being distinct objects is insufficient.
4. Integrate private UI via reviewed same-origin gateway or controlled CORS with CSP. Do not put session tokens into URLs, HTML, logs, browser storage, GitHub or public preview. Do not publish private source UI using GitHub Pages.
5. Complete verified bank/PSP evidence/receipts, provider-backed cash reconciliation, signed payment webhooks and real banking controls. Internal AR allocations cannot be called externally settled.
6. Implement and independently certify correct Saudi VAT country pack and ZATCA UBL/signing/clearance/reporting before issuing legal customer documents.
7. Rehearse backup restore and key recovery, performance/penetration tests, browser UAT, IFRS/SOCPA accounting review and explicit rollback/signoff.
8. SaaS paid billing, trial conversions, sector templates (medical, industrial, POS, payroll/WPS), mobile/AI and external integrations still need distinct approved acceptance.

**No auto-merge, real payment, governmental submission, tenant migration, Oracle deployment or changes to customer records performed by this branch.**
