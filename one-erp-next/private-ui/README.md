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


## Internal invoice maker/checker reviewer screen (development-only)

The separate \`invoice-approval-page.mjs\` and \`invoice-approval-client.mjs\` add a private RTL/LTR **independent reviewer** screen for an existing internal invoice source (migration 059/060). They are NOT mounted into the public preview, deployed or connected to a real login. A reviewer opens an exact immutable document reference with \`page.open({tenant, document_id})\`. The screen must load independently verified, authorized, tenant-scoped source evidence from a protected backend \`loadSummary\` callback; without it, approval fails closed. Display uses \`textContent\` (no HTML interpolation). A reviewer must explicitly acknowledge details before the reference-only HTTPS POST can be attempted.

The server route \`/api/v1/finance/internal-invoice-approvals\` already exists in development but remains **disabled by default** in the staging router. The client transmits only \`tenant\` and \`document_id\`, plus a memory-only short-lived JWT header. It never sends amounts, account numbers, raw SQL, permissions, cookies or bank details. It rejects invalid hosts, unreviewable sources, duplicate clicks and mismatched approval replies. All real authority still belongs to backend membership checks and PostgreSQL maker/checker triggers.

**Integration blockers:** A production-authenticated read-only invoice-source detail API that returns verifiable source/lineage, actual IdP/tenant route and restricted SQL roles, an approved operator UI flow, server-side replay/freshness checks, financial signoff, reviewed CORS/same-origin strategy, real staging acceptance and ZATCA compliance. A browser cannot guarantee immutable source authenticity or prevent all races by itself. Do **not** enable the approval route or claim statutory invoicing, payment settlement, Oracle deployment or production readiness.
