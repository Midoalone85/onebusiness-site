# ONE ERP NEXT 059 | Immutable approved invoice source (review-only)

**Status 17 October 2026:** independently tested PostgreSQL 14 implementation in branch `feature/finance-replay-private-http-20261017`, not mounted or deployed to Oracle.

## Design and trust chain

1. Trusted backend authenticates user and tenant using signed credentials, outside PostgreSQL. An internal operator prepares a source invoice as an exact **string-decimal** JSONB record in `one_invoice_source_requests`. The database trigger verifies the transaction-scoped actor and membership and refuses UPDATE/DELETE.
2. A **different authorized accounting user** approves via `one_invoice_source_approvals`. The database rejects self-approval, unauthenticated review and amendment/deletion of either record. Both tables enforce forced tenant RLS and block public grants.
3. `ApprovedPostgresFinanceSourceLoader` reads only joined independently approved records plus actual tenant chart, accounting period, currency precision and active membership from PostgreSQL. It strictly validates the approved source format (including finite decimal strings and line count) and never accepts account IDs, amounts or roles from the HTTP client.
4. `POST /api/v1/finance/internal-documents` uses only action, tenant UUID and document UUID. The server verifies signed bearer identities, selects a pinned database and uses the source loader to invoke existing atomic posting and replay wrappers. Migrations **057/058** seal same-reference results at transaction commit.
5. An identical request returns the existing internal document ID without an additional ledger journal. Changing any approved request identity or fingerprint fails closed. The approved source remains available for historical retries.

## Implemented now

- Migration: `one-erp-next/db/059_approved_invoice_sources.sql`.
- Backend: `one-erp-next/src/one_erp_core/approved_finance_sources.py`.
- Disposable PostgreSQL tests: `test_approved_finance_sources_postgres.py`, with separate approver, immutable source, role revocation, malformed exact amount, tenant scope and actual journal/invoice/replay; plus a signed HTTP route on real temporary PostgreSQL.
- Payment source loader deliberately **denies all requests** until an independently reviewed payment-source approval procedure and correct open-invoice snapshots / refunds are available.

## Mandatory live release blockers

- This migration **does not** create a public invoice drafting UI or HTTP request/approval endpoints. A separately authenticated, audited maker-checker service remains to be implemented, with evidence attachments and immutable approvals; the CI fixtures insert directly under a disposable administrator.
- The current SQL examples and tests operate with elevated disposable setup credentials. Provision production roles separately with only reviewed SELECT/INSERT/EXECUTE rights, no browser DB access, no superuser or BYPASSRLS, and verify PUBLIC / inherited permissions. Operator entitlement and revocation must be server-validated.
- No ZATCA, QR/UBL, invoice delivery, Saudi VAT tax decision engine, lawful credit-note issuance or bank settlement is performed. Actual official invoice numbering and compliance need country-pack approval and official integration.
- Test real HTTPS load-balancer proxy configuration, revoked token behavior, external IdP, MFA, route mounting, approved-staging lifecycle, retry across worker crash/restart and permission matrix.
- Run backup+restore, schema migration rehearsal with rollback, independent DBA/privacy/security review, invoice/report/stock reconciliations, browser acceptance and sign-offs. Keep both PRs draft; do not merge into `main` or deploy Oracle based on these tests alone.

**References:** [PR #43](https://github.com/Midoalone85/onebusiness-site/pull/43), [PR #44](https://github.com/Midoalone85/onebusiness-site/pull/44).
