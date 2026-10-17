# ONE ERP NEXT | Private Accounts Receivable Aging Acceptance
**Date:** 17 October 2026 | **Review-only:** draft PR #49, stacked above draft PR #48. No Oracle deployment and no production data changes.

## Shipped source-code functionality
- Migration `061_receivables_read_permissions.sql` registers `finance.receivables.view` in the independently approved permissions framework. Only the active company administrator and accountant roles are allowed by default. **Viewer is denied**. Explicit, previously approved per-user/role policies can override defaults according to the existing precedence engine.
- `/api/v1/operations/receivables?tenant=<uuid>&as_of=YYYY-MM-DD&limit=25&offset=0` is an authenticated HTTPS GET endpoint. The server verifies a signed identity and the exact tenant, rejects missing/duplicate/invalid as-of dates and extra fields, restricts pagination, and returns private `no-store` JSON with exact decimal strings. No payment processing or ZATCA requests.
- The SQL report is tenant-scoped and permissioned via `one_actor_has_permission`. It runs in a read-only repeatable-read PostgreSQL transaction as a nonsuperuser who may not BYPASSRLS.
- Its invoice rows reflect **issued customer invoices**, discounted by **applied internal customer payment allocations** and **issued customer credits** as of the effective report date. Applied payments are included only if both transaction date and applied-at date are on or before the as-of date. Credits similarly require both issuance and approval dates on or before the reporting date. No future posting backdated after the report date can leak into past snapshots.
- Exact amounts per invoice include gross, applied internal allocations, issued credits, outstanding and aging buckets (current, 1–30, 31–60, 61–90, over 90 days). Fully internally allocated invoices are labeled `no_open_balance_internal`, **not** 'bank confirmed' or 'externally settled'. Different currency balances are not converted or combined.
- The staging router sends sensitive receivables to `finance_report_connections` instead of the normal operational `operations_connections`, maintaining separate reader SQL credentials. Readiness and signed HTTPS checks still apply.

## Confirmed automated scenarios
- Pure validation: age boundaries, exact decimal precision, settled/internal-only status, abnormal credits/payments and fraudulent/NaN values.
- WSGI: date-required, foreign company rejected before DB, duplicate/smuggled parameters denied; no unauthenticated disclosure.
- Actual disposable PostgreSQL 14: purchase receipt/GRNI + WAC COGS + issued customer invoice (SAR 200) + an **internally applied, NOT bank-verified** SAR 60 receipt. AR dated before internal receipt reports SAR 200 and after reports SAR 140. Late-date aging reports overdue. An actual separate restricted SQL login is denied UPDATE to customer invoices and unknown actor cannot view the report.
- Staging router unit tests verify report role factory, not the general operations reader login.

## Critical remaining tasks and release blockers
1. Actual Oracle-hosted **staging** with DNS, real TLS, server process, IdP, MFA and independent tenant database inventory.
2. Least-privilege dedicated operations/report/source-maker/source-approver/posting SQL roles, secure key rotation, exact GRANT/REVOKE evidence. Tests use temporary SQL roles, not production grants.
3. Protected accounting and warehouse browser workflows (customers, sales order, delivery, GRNI, COGS, invoice, refund, AR aging). Staging UI and domain pages still need authenticated end-to-end validation.
4. Approved bank/PSP payment evidence + independent cash matching and reconciliation; existing internal payment posting has **no live bank confirmation** and can never be treated as proof funds actually arrived.
5. Versioned Saudi VAT and ZATCA integration, invoice number policy, UBL/QR/stamping/reporting/clearance and regulatory acceptance. Existing source snapshots are internal accounting artifacts ONLY.
6. SaaS Trial→Paid subscription provider, actual charge/tokenization, webhook signatures, billing invoice issuance and retries/reversals. Mock payment flags do not count as a successful charge.
7. Hospital/clinic/lab/pharmacy, manufacturing, POS, HR/WPS, maintenance, multi-country, AI, reports and mobile flows each require independent completion and scoped acceptance.
8. Run real backup-restore drill, incident and rollback tests, load/pen testing, external accounting/security review and a formal release signoff before any production merge/deploy.

**Safety:** Do not merge the stacked draft PRs directly to `main`; no customer migrations, ZATCA submissions, payments, or Oracle production changes have been carried out by this package. Passing CI validates developer/test paths only.
