# ONE ERP NEXT release readiness | 2026-10-17

## Verified integration commit
- Branch: integration/one-erp-next-finance-20261017
- Main comparison: 376 commits ahead, zero behind, 175 changed paths; 51 db SQL migration paths.
- GitHub finance CI: https://github.com/Midoalone85/onebusiness-site/actions/runs/39019047262 - Python 3.11 and 3.13 both success.
- Integration gates: https://github.com/Midoalone85/onebusiness-site/actions/runs/39019047288 - four jobs success.
- Brand guard: https://github.com/Midoalone85/onebusiness-site/actions/runs/39019047243 - success.
- Jekyll build: https://github.com/Midoalone85/onebusiness-site/actions/runs/39019047174 - success.

## No automatic production approval
Automated tests use a disposable PostgreSQL test database with elevated setup privileges. No evidence yet of production-data migration, authenticated Oracle deployment, real bank transfers, independently approved legal/tax signoff, IdP configuration, backup/restore rehearsal or penetration/security review.

## Human review gates before main merge
1. Review the 51 SQL migration paths, tenant RLS, source-level DENY, and finance role grants.
2. Review the 175 changed paths against current main, specifically .github workflows and Oracle bridge. Do not change owner privacy guard.
3. Identify CI-only PostgreSQL privileges and ensure they cannot be used for production service accounts.
4. Verify invoices/credit notes/stock reversals preserve idempotency and balanced GL; re-check ZATCA in an actual isolated test environment.
5. Ensure out-of-band independent reviewer approval for the bank candidate process, verify never marked cash-paid by mere candidate creation.
6. Confirm user authentication, tenant database isolation, backups/restore and HTTPS with approved deployment plan.
7. Require separate sign-off and exact integration SHA; do not force-push or auto-deploy.

## Release disposition
PASS: isolated CI as configured.
PENDING: code/security review, real-service smoke tests, migration rehearsal and production acceptance.
DO NOT DEPLOY to Oracle solely based on CI.
