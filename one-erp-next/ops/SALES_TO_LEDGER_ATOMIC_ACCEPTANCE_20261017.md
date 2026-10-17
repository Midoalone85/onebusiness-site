# ONE ERP NEXT | Delivered-sales invoice → GL & allocation review package

**Date:** 17 Oct 2026. **Status:** draft internal finance code and disposable PostgreSQL 14 integration tests ONLY. No tenant/customer production migrations, no Oracle staging deployment, bank settlement, regulated Saudi invoice issuance, or statutory signoff.

## Independent ERP business transaction exercised

1. An authorized company admin creates a purchasing order and confirms it.
2. Physical purchase receipt and inventory valuation post **DR Inventory / CR GRNI** on the purchase movement.
3. The confirmed customer sales order ships goods and posts weighted-average COGS **DR COGS / CR Inventory**, atomically with dispatch.
4. A separately-reviewed **internal accounting-tax policy reference** and named invoice intent are immutable in migration `060_sales_invoice_approved_intents.sql`. This DOES NOT constitute a verified Saudi country pack or ZATCA clearance.
5. The trusted backend reads the real order and product lines, shipped quantities, previous posted invoice allocations, price, customer and currency from PostgreSQL. It locks the order and rows, excludes quantities already invoiced, and refuses draft/unshipped/unvalued orders. The client never supplies quantities/accounts/mapping.
6. The invoice maker creates frozen source records and exact immutable commercial lineage in one SQL transaction (migrations 059/060). A different active accounting reviewer authorizes issuance. The separate reference-only HTTPS approval route is disabled until operator configuration.
7. The internal finance command posts sales journal, invoice, audit and replay receipt; SQL `one_link_sales_invoice` matches the **whole** issued invoice against the original dispatched lines, rejecting discrepancies and over-billing. All steps use the one parent PostgreSQL transaction; failure rolls back invoice, GL posting and replay claim together.
8. Repeated same-reference requests return the sealed outcome. Concurrent requests for two different approved invoices against the same already dispatched quantity serialize on the order and cannot both invoice the quantity.
9. The integration fixture reconciles end balances: `DR Inventory 400 / CR GRNI 500 / DR COGS 100 / DR AR 200 / CR Sales 200`, exactly balanced. Unit values are **test example only**.

## Code changes

- `db/060_sales_invoice_approved_intents.sql`: immutable reviewer-attested internal invoice intentions, source-line lineage records, FORCE tenant row-level security, append-only triggers, no PUBLIC table/function access.
- `src/one_erp_core/commercial_invoice_builder.py`: PostgreSQL-only confirmed/shipped/valued/unbilled sale source snapshot; rejects bad source mapping, inactive reviewer, unsupported currency precision and invalid amounts.
- `src/one_erp_core/invoice_source_workflow.py`: extend `BuiltInvoiceSource` with validated source line lineage, atomically persist it with immutable source request.
- `src/one_erp_core/approved_finance_sources.py`: returns the original saved lineage for posting and retry.
- `src/one_erp_core/finance_replay_commands_wsgi.py`: re-check valuation at posting, atomic commercial allocation, and durable replay source mapping verification.
- `tests/test_approved_finance_sources_postgres.py`: real PostgreSQL 14 purchase+WAC+sale+maker/checker+issue+replay and negative/parallel accounting regression. No live data.

## Explicit limitations and next release gates

- Fiscal treatment in test fixtures is `out_of_scope` **only for a non-regulatory INTERNAL journal test**. This is not a valid substitute for Saudi VAT on taxable goods. The country pack must independently determine category/rate/exemption, taxpayer identity, QR, UBL2.1, cryptographic stamping, B2B clearance / B2C reporting, full ZATCA phase requirements and legally assigned invoice numbering. Nothing in this PR talks to ZATCA or claims official acceptance.
- The source intent INSERT is a **trusted backend/DB setup primitive**, not yet a customer-facing UI. Identity directory, audited finance delegation, real tax policy versioning/signature, maker/checker UI, corrections/cancellations and regulated retention remain required.
- The original inventory opening in earlier test revisions was **replaced with actual purchase receipt+stock valuation**. Still required: full customer commercial order screen, delivery-document UX, landed-cost/FX, closing controls and audit reconciliation with all sector packs.
- Real independently restricted source-make/source-approve/read/post SQL roles and physical one-database-per-tenant registry under Oracle SaaS infrastructure are not installed.
- Payment source loader remains **explicitly denied**. To complete Accounts Receivable, build trusted frozen payment allocations and verified bank/PSP confirmations with independent reviewer and idempotency before any real financial capture.
- SaaS subscription trial→paid, invoices, renewals and webhooks require a real provider; simulated payment flags may not trigger paid access.
- Other domains such as hospitals, pharmacies, laboratories, manufacturing, payroll/WPS, POS, CRM, ONE AI, multilingual screens, reporting and real customer provisioning require implementation, business acceptance and realistic end-to-end tests. Passing finance tests cannot imply these are complete.
- No automated deployment, merge, production role grants, tax submission, customer emails or money movements authorized or attempted. Keep draft PRs and require a reviewed Oracle staging rehearsal, actual restore test, threat/penetration review, accounting/SOCPA/IFRS and KSA regulatory signoff plus rollback runbook.

## Validation evidence

Use the HEAD commit workflow run attached to **draft PR #48**; successful prior HEADs alone are not acceptance. Tests include Python 3.11 and 3.13 with disposable PG14, UI regressions, static release/privilege checks and source privacy.
