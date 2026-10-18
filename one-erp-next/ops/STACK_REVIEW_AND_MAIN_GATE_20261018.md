# ONE ERP NEXT: branch integration and commercial staging acceptance
Date: 2026-10-18  
Status: **AUDIT AND TEST PLAN ONLY**. PRs remain draft. Do not merge into `main`, deploy Oracle, or issue production invoices from this document.

## Independently retrieved GitHub topology

- `main` HEAD at inspection: `50c01074b1dc7e38ad5a81738d20f2c3d82463db`
- `main` -> PR **#38**: `integration/one-erp-next-finance-20261017` (draft, integration preservation).
- The SAME integration head is also submitted via PR **#41** toward `one-erp-staging`, **not** `main`. The existing `one-erp-staging` branch differs substantially; never treat #38 and #41 as two independent incremental commits.
- Integration head -> **#43** finance replay -> **#44** signed replay HTTP -> **#45** staging readiness -> **#46** private WSGI composition -> **#47** maker/checker -> **#48** delivered-sale/ledger atomic link.
- **Fork A:** #48 -> **#49** receivables aging -> **#52** bilingual receivables UI -> **#53** combined private finance workspace.
- **Fork B:** #48 -> **#50** independent reviewer UI -> **#51** invoice source review and permission. #53 selectively integrates the source-review work of Fork B on Fork A, so there is *not* a clean ancestor link from #51 to #53.
- #53 currently targets **#52 feature branch**, not `main`. All 44-53 PRs are draft/unmerged as observed.

At the earlier inspection, `main..#53` was **587 commits ahead and 0 behind**, representing **255 changed files**, while `one-erp-staging` versus integration had **452 commits ahead and 419 behind**. Recalculate before every integration attempt; these are historical snapshots, not permanent counts.

## Exact-file comparison of sibling work (not a substitute for code review)

For the 12 paths changed directly in #51, compare the file SHA in the #51 HEAD with #53 HEAD. The following **eight exact SHA matches** were verified at inspection:
- `db/062_invoice_source_review_permission.sql`
- `ops/INVOICE_REVIEW_SOURCE_062_20261017.md`
- `private-ui/invoice-review-client.test.mjs`
- `src/one_erp_core/invoice_review_source.py`
- `src/one_erp_core/invoice_review_wsgi.py`
- `tests/test_invoice_review_source.py`
- `tests/test_invoice_review_wsgi.py`
- plus the common source-review implementation artifact count is **seven?** Verify the list against the exact filenames below before declaring a full 8/12 equivalence.

The four #51 paths whose blobs **are NOT identical** in #53 are:
- `private-ui/invoice-approval-page.mjs`: consolidated stricter source validation and post-approval timeline refresh.
- `private-ui/invoice-review-client.mjs`: shared validation and numeric rejection changed; independently review for regressions.
- `src/one_erp_core/staging_router.py`: adds receivables and activity routing, independent reader registry; independently verify the actor/tenant/readiness boundary.
- `tests/test_approved_finance_sources_postgres.py` and `tests/test_staging_router.py`: updated tests and extended acceptance cases.

**Correction:** this is actually **7 identical / 5 changed** paths when counted against the 12-file list. The exact counted list above has 7 items. Use this corrected count in release discussions. A quick line-set difference does NOT establish complete semantic preservation of the 5 changed paths. Keep independent code review blocking.

For #52's four files, `receivables-client.mjs`, `receivables-client.test.mjs` and `receivables-page.mjs` were **identical SHA** to #53. Its `private-ui/README.md` in #53 was expanded, with original lines present. This is a text-level comparison, not an API security audit.

## Non-negotiable safe merge process

1. **Freeze reviewed exact commit SHAs** for `main`, both sibling tips and combined PR #53; re-review on any change, especially SQL migrations/permissions.
2. Review code against **both** #51 and #52 branch tips; do not naively merge #51 on top of #53 or apply both #38 and #41 as separate patches.
3. Build a new **disposable dry-run integration worktree** from current `main`. Apply/merge the combined branch only after validating its full ancestry, dependency order, migration manifest, license and protected main UI/Oracle workflow assets. Do not use `-X ours` or `-X theirs` as an automatic conflict resolver.
4. Run full Python 3.11 / 3.13 and PostgreSQL 14/16/17 CI and the private RTL/LTR UI, preview, brand, migration, blocked-route, and concurrency tests against the exact combined **candidate tree**. Include no-skips and no-test-collection-error checks. The prior isolated branch green runs do not certify the dry-run merge result.
5. Perform independent restricted-role database review: PUBLIC/grant chains, actor spoofing, RLS enforcement, physical database segregation, maker/checker, source lineage, GL/stock reconciliation, idempotency under failure and simultaneous users.
6. Deploy only to a **separate, isolated Oracle staging** after a reviewed plan with trusted secrets/IdP/MFA/DNS/TLS, verified customer DB isolation, backup/restore and rollback; use read-only staging smoke first.
7. Manual financial, regulatory and security acceptance before `main` merge. Production tax/ZATCA, bank confirmation, paid subscriptions and high-risk write routes each require independent integration and signoff. CI cannot substitute.
8. Keep the one-logical-core decision and **defer autoscaling**. No new replicas or paid Oracle resources in this PR.

## Operational no-go list

- No customer invoices as Saudi statutory/ZATCA documents from the internal finance prototype.
- No bank-settled, cash-confirmed, or provider-paid claim from manually applied internal postings.
- No raw tenant connection strings/client-selected SQL roles. No secrets or signed tokens in GitHub text.
- No publication of private company reports through the public fictional-data GitHub Pages preview.
- No automatic production merge, financial migration or deployment from feature-branch PRs.

## Evidence and caveats

Source artifacts for this review: PRs #38, #41 and #43-53, comparison of real repository refs and exact file blobs, latest `one-erp-next` integration tests, and the project runbooks. Actual Oracle operator session, real tenant login, deployed isolated databases, live bank/ZATCA and backup restores **were not accessed or validated**. This report cannot authorize launch.
