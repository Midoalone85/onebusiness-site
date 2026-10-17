# ONE ERP NEXT integration baseline (2026-10-17)

Base: main. This branch is NOT a deployable release.

## Verified status
- Finance draft PR #36: Finance draft PR #36 (repository-local)
- Full finance suite for its source branch passed: 543 tests on Python 3.11 and 543 on Python 3.13 in GitHub Actions run 39012533223 (repository-local) .
- main and finance feature 056 have diverged: 19 commits unique to main, 359 unique to feature 056.
- Reverse comparison identifies these main-side files to protect:
  - .github/workflows/one-erp-brand-guard.yml
  - .github/workflows/one-erp-oracle-bridge.yml
  - .github/workflows/one-erp-preview-tests.yml
  - DOMAIN_SETUP.md
  - one-erp-preview/index.html
  - one-erp-preview/tests/navigation.test.cjs
  - one-erp-preview/ui.css
  - one-erp-preview/ui.js
  - one-erp-preview/workspace/index.html

## Safe integration gates
1. Preserve the preview UI, brand guard and Oracle bridge workflows from main.
2. Bring in predecessor finance branches in reviewed migration order. Do not flatten, force-push or blindly copy 171 differing files.
3. Audit role grants, tenant isolation/RLS, migration ordering, immutable ledgers and maker-checker.
4. Run fresh CI on the final integration branch, Python 3.11 and 3.13 with isolated PostgreSQL 14; run preview/navigation and brand checks.
5. Require explicit reviewer approval before merging into main. Separate Oracle deploy after backup/security/HTTPS gates.

No finance feature was merged into this branch by this documentation commit.
