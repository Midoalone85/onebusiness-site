# ONE ERP NEXT private operations page (not deployed)

This folder is **private application source only**. Do not add it to the public GitHub Pages demo. `operations-page.mjs` mounts Arabic RTL / English LTR navigation for sales, purchases, inventory and returns, accepts a trusted in-memory access token provider and a configured HTTPS API origin, and renders API-provided rows as DOM text only.

Example for an authenticated *private* app shell:

```js
import {mountOperationsPage} from "./operations-page.mjs";
const page = mountOperationsPage({
  root: document.getElementById("operations"),
  baseUrl: "https://private-api.example.test/",
  getToken: () => trustedSessionProvider.getShortLivedAccessToken(),
  language: "ar"
});
await page.load("sales");
// On sign out: page.dispose();
```

Security: no API host or OIDC provider is configured yet; no login implemented; no database operations or payment posting. The server must verify OIDC JWTs, tenant membership and database permissions. Client-side checks are UX, never authorization. Only read endpoints exist. The read API currently may not allow browser CORS, so integration requires a vetted same-origin private app gateway or backend-for-frontend. Do not enable wildcard CORS or copy JWTs into browser storage.

The public ONE ERP sample site remains a fictional-data preview and must stay independent of real tenant data.


## Private receivables aging page (review-only)

The independent \`receivables-client.mjs\` and \`receivables-page.mjs\` implement an Arabic RTL / English LTR read-only aging screen for the actual **tenant-scoped** \`GET /api/v1/operations/receivables\` service in migration 061. They are **not deployed** and must not be linked to the public fictional-data ONE ERP preview.

An authenticated private shell injects \`root\`, trusted HTTPS \`baseUrl\`, an exact tenant UUID, a short-lived memory-only \`getToken\` provider and explicit \`as_of\` (YYYY-MM-DD). Example:

\`\`\`js
import {mountReceivablesPage} from "./receivables-page.mjs";
const page=mountReceivablesPage({
  root:document.getElementById("receivables"),
  baseUrl:"https://private-api.example.test/",
  tenant:activeVerifiedCompanyId,
  as_of:"2026-10-17",
  getToken:()=>trustedSessionProvider.getShortLivedAccessToken(),
  language:"ar",
});
await page.load();
\`\`\`

The client accepts only a signed GET, bounded pagination and backend \`one_erp_next_postgresql\` read-only results matching requested tenant/as-of. It validates every returned row and exact gross minus applied internal receipts minus issued credits equals outstanding with integer millionths/BigInt (no floating point amounts). Current-page totals are grouped **separately per currency**, never falsely represented as whole-ledger totals or converted. DOM uses \`textContent\` exclusively, with explicit note that internally applied receipts are **not bank-verified**. Failed / revoked access clears previously visible balances; paging aborts stale requests. Close with \`page.dispose()\`.

Still needed: independently authenticated private host and CORS/same-origin gateway review, production DBA/IdP/RLS, real user acceptance, complete filter/print/export, independent bank/PSP settlement, ZATCA country pack, and Oracle hosting/rollback gates. No production release is approved.
