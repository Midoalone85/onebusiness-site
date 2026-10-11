# ONE Business domain cutover checklist

## Current topology
- GitHub Pages static marketing: ONE Business + /one-erp/ + /one-erp/plans/
- Oracle application: public HTTP and route /app. HTTPS is NOT live.
- Do not put customer financial data or passwords into an HTTP connection.

## Domain onebusiness.site
As of 2026-10-11 current public records observed:
- apex A: 162.255.119.180 (not GitHub Pages)
- www A resolves to hosting/forwarding IPs
- erp.onebusiness.site: no A record
- MX and SPF exist for Namecheap email forwarding. Preserve those.

## Required work with the domain owner
1. In GitHub repo Settings > Pages > Custom domain, set onebusiness.site.
   This repository deploys using a custom GitHub Actions workflow:
   a CNAME file alone is NOT enough to register this domain.
   GitHub Pages may need time for HTTPS certificate issuance.
2. At the authoritative DNS host, remove only conflicting apex web and www web forwarding records.
   Add FOUR apex A records:
   @ 185.199.108.153
   @ 185.199.109.153
   @ 185.199.110.153
   @ 185.199.111.153
   www CNAME midoalone85.github.io (the DNS target is a provider alias, not customer branding).
   Keep MX, SPF/TXT, any business mail verification and DNS records not related to the site.
3. Validate using independent resolvers (1.1.1.1, 8.8.8.8), 200 response for
   https://onebusiness.site/ and /one-erp/plans/ and no browser certificate warning.
4. Do NOT enable public production login or claim readiness for Oracle ERP yet.
   Set erp A record to 141.147.137.107 only after SSL/TLS reverse proxy is ready.
   Expose only HTTPS 443 after cert issuance, forward securely to ONE ERP v3 public gateway,
   preserve proxy forwarded host and scheme, test session cookies and login.
5. Audit all absolute HTTP Oracle links in marketing and replace them with
   https://erp.onebusiness.site/app and appropriate secure commercial routes after TLS is verified.

## Business/compliance acceptance tests pending
- Customer invoice -> tax -> posted journal -> aging -> ledger -> trial balance -> P&L.
- Vendor bill -> purchase -> stock valuation -> accounts payable.
- Bank import/statement -> match -> reconciliation -> locked period.
- Country profile KSA vs Egypt, fiscal year and multiple legal entities.
- ZATCA Phase 2 external onboarding / integration testing; do not claim certification before success.
- Data isolation, roles, backup restore drill, mobile navigation and invoice PDF review.
