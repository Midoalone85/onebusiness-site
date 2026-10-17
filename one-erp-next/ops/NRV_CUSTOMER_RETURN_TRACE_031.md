# ONE ERP NEXT: phase 031 | NRV customer return trace

Date: 2026-10-15. Branch: feature/one-erp-smart-finance-20261015.
CI/disposable PostgreSQL 14 only. No Oracle change, production deployment,
official KSA tax credit issuance, or IFRS/SOCPA certification.

## Added
- db/031_nrv_qa_source_report.sql offers an authorized read-only
  source-linked QA return control: original sale release, accepted RMA
  quantity, posted gross COGS restoration, recognized replacement NRV
  allowance, and quarantine stock/valuation must reconcile.
- Tests inspect a posted two-unit customer return (gross cost SAR 25,
  new allowance SAR 9, net QA carrying value SAR 16) and enforce
  company-level access restrictions.
- Source mismatch is returned as reconciliation false, not silently
  treated as a financial statement amount.
- A returned item is never automatically restored to sellable inventory.
  Follow-up QA disposition and formal customer financial refund/credit
  require distinct authorization, valuation, tax and payment workflows.

## Release blockers
Actual signed identity and least-privilege tenant database roles;
automated quality inspection disposition and source cost layers for
multiple returns; customer credit note matching under local VAT and
ZATCA; partial refunds and customer payment allocations; serial/lot
traceability; robust period close, IFRS note disclosures and review;
staging deployment and production restore tests.

This control is a development proof, not a production-safe customer
returns screen. Keep the PR draft and do not merge to production.
