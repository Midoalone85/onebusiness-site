# ONE ERP Next | Session checkpoint | 2026-10-15

## Project and source of truth

ONE ERP **Next** is an original, independent ERP core. It is **not** the
legacy Oracle-hosted Odoo environment. Do not deploy, merge into production,
alter live customer data, or treat the static UI preview as connected software.

GitHub repo: `REPOSITORY_OWNER/onebusiness-site`
Active finance/commerce branch:
`feature/one-erp-smart-finance-20261015`
Draft PR: https://github.com/REPOSITORY_OWNER/onebusiness-site/pull/20
Last commerce-code commit before this checkpoint: `e79a827fafc5874123a967878177ae02535ce548`.

## User's scope and priorities

- Develop a **general-purpose commercial ERP** suitable for ordinary
  trading/distribution businesses, not only a hospital program.
- Shared financial core with sales, purchasing, inventory, CRM, HR, POS,
  reports, AI; sector-specific optional packs for hospital, clinic, lab,
  pharmacy, factory, maintenance, etc.
- Independent modules, compact premium blue UI with top menu and vertical
  dropdowns; each selection gets its own address/page, bilingual Arabic RTL
  and English LTR. Existing static UI has fictitious data only.
- Dedicated database per customer, security, auditable accounting,
  appropriate Saudi localization, and commercial readiness before rollout.
- Never accidentally resume legacy Odoo development when asked to continue.

## Proven through the last passing CI run

- Financial core and tenant-provisioning guard: `199` tests on each of
  Python 3.11 and Python 3.13 against disposable PostgreSQL 14.
- Run https://github.com/REPOSITORY_OWNER/onebusiness-site/actions/runs/38671106578
  concluded success before commerce work.
- Tenant physical-inventory verifier added and covered by CI; attested
  real-world backup/restore/HTTPS readiness is NOT implemented.

## Newly committed in the interrupted commerce work

1. `src/one_erp_core/commerce.py`
   - Product, warehouse, sales/purchase order and line policies.
   - Decimal quantity and price checks, tenant scope, net totals.
   - Read-only proposed purchase receipts and sale dispatches that check
     unreserved stock, partial fulfillment and admin permissions.
2. `src/one_erp_core/tenancy.py`
   - New `PROCESS_STOCK` action, narrowly granted to company admins
     under existing role policy; not conflated with accounting permission.
3. `tests/test_commerce.py`
   - 21 deterministic unit tests for commercial policies.
4. `db/005_commercial_stock.sql`
   - PostgreSQL 14 experimental tables, FORCE RLS, tenant-bound foreign keys,
     append-only stock events, draft/confirmed orders, restricted helper SQL
     routines, transactionally updated balances and fulfillment.
5. `tests/test_postgres_integration.py`
   - CI-only grants and 5 new PostgreSQL tests for receiving, dispatching,
     insufficient stock, duplicate event IDs, tenant/actor checks and
     unauthorized mutation.

## Verification status at session save

The last 199-test pass is from BEFORE commerce changes. Commerce commit
`e79a827` started GitHub Actions runs:
- https://github.com/REPOSITORY_OWNER/onebusiness-site/actions/runs/38671886680
- https://github.com/REPOSITORY_OWNER/onebusiness-site/actions/runs/38671881520

At the moment of this checkpoint, runs were still in progress.
**Do NOT claim commerce code is tested or accepted until checking
the latest conclusion and reviewing failure logs.** Expected test-count
increase is 21 unit tests + 5 DB integration tests; actual outcome is
authoritative.

## Resume exactly here

1. Fetch both workflow conclusions above. If either fails, read job logs,
   fix the commercial SQL/domain logic and CI fixtures on the same branch,
   and rerun. Verify both Python 3.11/3.13 with real disposable PostgreSQL.
2. Pay particular attention to `005_commercial_stock.sql` trigger guards,
   `one_confirm_commercial_order` and `one_fulfill_commercial_line`,
   transaction rollback/idempotency and any mismatch between SQL and domain.
3. Review SQL privilege isolation: CI broad table grants are not production
   grants, session GUCs are not authentication. Prefer a narrow signed-in
   backend operation with dedicated database roles before service exposure.
4. Document the verified commerce foundation under
   `ops/COMMERCIAL_FOUNDATION.md`; update PR #20 with actual CI results.
5. Continue commercial-first vertical slices: reservation lifecycle,
   stock valuation & cost of goods sold, quotations -> sales orders ->
   picking/dispatch -> invoice/journal, and purchasing -> receipt ->
   vendor bill, with unique idempotent operations and tenant isolation.
6. Only then integrate with the authenticated commercial UI and introduce
   optional medical/sector packs on the shared core.

## Deployment and completeness

Neither these commits nor GitHub CI are evidence of a running standalone
ONE ERP production service. No Oracle production code/database was changed
by the latest commerce work. The app remains an engineering prototype.
