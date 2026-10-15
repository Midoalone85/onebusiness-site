# ONE ERP Next Finance: deployment gates

**Status: experimental / not production ready.** Work lives on `feature/one-erp-smart-finance-20261015`, independent of Odoo and Oracle production. No database migration has been applied to the live server.

## Build order

1. Trusted user authentication, tenant identity and audited provisioning.
2. Chart of accounts, fiscal periods and double-entry journal policy (Python domain rules).
3. PostgreSQL transactional persistence, database tenant policies, posting checks and append-only audit events (initial prototype).
4. Real DB integration tests and first implementations of admin-only period closing and exact source-linked reversals (in progress); concurrency/retry and permissions hardening still needed.
5. Read-only explainable bank-match proposals (prototype); actual bank feed import, settlement and approval audit not yet built. Then invoices, credit notes, receivables/payables, and payments.
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

Real sign-in, tenant provisioning, billing, web UI, approved ZATCA integration, invoice posting, receipts/payments, production-grade period close/reopen policy, approved bank reconciliation, FX rates, VAT engine, cashflow, polished PDF/interactive dashboards, medical packs, production deployment, certified compliance.

## Additional finance foundation (migration 002)

- Period close: requires company-admin membership, locks period row, records close timestamp/actor in append-only log, and prevents posting into the closed period. Existing draft journals remain drafts but cannot be posted after closure; workflow to resolve/reopen them must be designed before production.
- Reversals: create a *new* draft with same tenant/currency, original journal reference, inverse lines, deterministic idempotency key; database validates full exact line-by-line reversal and uniqueness.
- Bank reconciliation proposals: exact signed amount/currency and bounded dates only, human-reviewed, deterministic rationale; no automated ledger mutation or silent approvals.
- These are prototypes in a shared-tenant test schema. Privilege hardening, real identity proof, and external bank statement ingestion remain outstanding.

The pipeline `.github/workflows/one-erp-next-finance-tests.yml` runs Python tests and disposable PostgreSQL 14 integration tests. Check the actual workflow conclusion before calling the change verified.


## Invoice-to-payment vertical slice (experimental migrations 003 and 004)

- A country-pack-reviewed invoice or credit-note draft produces a balanced ledger journal. Money uses Decimal; invoice line base, discounts and tax amounts are rounded by an explicit line-level half-up policy. Zero-rated, exempt and out-of-scope remain distinct categories even when tax due is zero.
- Country tax compliance is NOT implemented. The demonstration standard rate is not a statutory rate engine. A production country pack must determine effective rates, recoverability, exemptions, legal numbering, fiscal metadata, archiving and ZATCA/gateway interoperability.
- Migrations 003/004 persist source-document snapshots, internal approvals, posted journals, payments and invoice allocations in one PostgreSQL transaction per document. Failed validation rolls back the whole transaction.
- Issued internal snapshots and applied payment allocations are immutable. Credit notes and approved payments both reduce the same original invoice balance, with a locking and bounded-allocation policy. A credit note cannot exceed the invoice's remaining gross after approved payments.
- Payments here represent internally recorded settlement, not confirmed banking transactions. Bank-feed connectors, treasury controls, approval workflow, reconciliation and external proof of receipt are required.
- Receivable/payable aging has deterministic current / 1-30 / 31-60 / 61-90 / 90+ buckets, supports historical as-of cutoff dates, and attaches original invoice, credit and payment identifiers to each row. Reporting callers MUST source consistent issued/applied records and avoid mixing tenants/currencies. PDF and interactive dashboards remain future work.
- Database custom transaction settings for tenant, actor and function context flags can be set by any account with raw SQL access. They are NOT unspoofable security principals. No untrusted user may receive a SQL connection, direct table DML, or function execution. Before production, implement trusted backend authentication, per-tenant DB credentials, table-level least privilege, secure stored procedures or a hardened service-only role, and external audit.
- Dedicated database per tenant remains the planned production security boundary; shared-tenant RLS and broad CI fixture grants are for prototype testing only. Do NOT deploy these test privileges.
- The development branch and GitHub test runner do not establish that the system is deployed or that any invoice is legally valid in Saudi Arabia.


## Read-only finance reports and presentation (current prototype)

- The balance sheet derives assets, liabilities, equity and *all remaining unclosed revenue/expense* from original posted journals, with source journal IDs for investigation. This is not yet a formal fiscal-year-close or audited-report engine.
- Account-by-account general ledger detail provides opening balance, chronological debit/credit movement, running balance, closing balance and original journal references. Debit-minus-credit is a signed GL convention, not a bank statement representation.
- Finance report domain access uses membership checks and refuses mixed tenants, currencies, duplicate or imbalanced posted source entries. Trusted identity and complete database snapshots are REQUIRED; client-supplied memberships are not authentication.
- The presentation module creates escaped, responsive Arabic RTL and English LTR HTML with print-oriented A4 layout and a visible accounting-equation discrepancy indicator. No PDF server, reporting endpoint or final graphical web dashboard is deployed.
- Comparative P&L inspects adjacent equal-duration periods and highlights expense growth, revenue decline and loss based on caller-chosen Decimal thresholds. These are rule-based explanatory flags, NOT fraud accusations, forecasts or AI certainties.
- Publishing production financial statements requires legal entity name/identity, reporting currency rules, opening/closing balances, treatment of prior-year earnings and equity, consolidation, audit approvals, backups, user-role security and end-to-end browser tests.
