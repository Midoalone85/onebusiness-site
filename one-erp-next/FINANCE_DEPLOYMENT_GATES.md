# ONE ERP Next Finance: deployment gates

**Status: experimental / not production ready.** Work lives on `feature/one-erp-smart-finance-20261015`, independent of Odoo and Oracle production. No database migration has been applied to the live server.

## Build order

1. Trusted user authentication, tenant identity and audited provisioning.
2. Chart of accounts, fiscal periods and double-entry journal policy (Python domain rules).
3. PostgreSQL transactional persistence, database tenant policies, posting checks and append-only audit events (initial prototype).
4. Real DB integration tests, concurrency/retry tests, permissions, closing periods and reversals.
5. Invoice and credit-note subledger, receivables/payables, payments and reconciliation.
6. Country-specific taxes, e-invoicing adapters, financial year close and multi-currency FX.
7. Source-linked reports: trial balance, profit and loss, balance sheet, cashflow, comparisons and visual drill-down.
8. Intelligent anomaly detection/financial explanations; AI suggestions never silently post or alter entries.
9. Tenant onboarding, billing and trial-to-paid by verified gateway webhooks.
10. Specialized sector packs (hospital/clinic/lab/pharmacy and others) only after common ledger stability.

## Runtime security requirements

- The current `one.tenant_id` and `one.actor_id` session settings **are not authentication**. PostgreSQL accepts GUC settings from connected clients. Only a trusted backend with a dedicated restricted DB role may construct them from verified user sessions. Never expose raw DB credentials or SQL endpoints to tenants.
- RLS is defense-in-depth, not an alternative to tenant-specific database isolation. Production architecture still needs an explicit **dedicated database per customer** decision and tests. A shared ledger prototype is not proof of full tenant isolation.
- CI fixture role permissions are intentionally broad for exercising policy and triggers. Do **not** copy them to production; audit and posting privileges need narrowly scoped runtime roles/procedures before deployment.
- Currency precision must agree in Python and PostgreSQL. Unknown currencies, FX, and rounding policies require explicit reviewed country packs.
- Creation of PostgreSQL constraints and checks must be tested against an actual PostgreSQL version, not only Python fakes.
- Every posting requires a unique (tenant, idempotency key); a retry must look up the original result, not create a second financial effect.
- A posted journal is not deleted or edited; corrections use linked reversing and replacement entries.
- Audit records are not a complete tamper-evident forensic log yet. Add privileged access audit, backups, recovery drills, and external immutable retention.
- Never publish credentials, admin reset tokens, customer data, or private secrets in GitHub.
- No automatic deployment or merging to production from this feature branch.

## What is not yet implemented

Real sign-in, tenant provisioning, billing, web UI, approved ZATCA integration, invoice posting, receipts/payments, closing periods, FX rates, VAT engine, cashflow, polished PDF/interactive dashboards, medical packs, production deployment, certified compliance.

The pipeline `.github/workflows/one-erp-next-finance-tests.yml` runs Python tests and disposable PostgreSQL 14 integration tests. Check the actual workflow conclusion before calling the change verified.
