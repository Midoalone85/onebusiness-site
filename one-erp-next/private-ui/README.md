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
