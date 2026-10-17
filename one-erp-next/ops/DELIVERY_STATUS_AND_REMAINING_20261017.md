# ONE ERP NEXT | Verified scope and remaining delivery gates

Updated: 17 October 2026. Source: review-only branch `feature/finance-replay-private-http-20261017`.
This is the independent Python/PostgreSQL ONE ERP NEXT product, **not** the legacy Odoo/Oracle ERP service.
No completion percentages or promised release dates are implied.

## Development evidence, not production evidence

| Workstream | Built/tested in source | Missing for real customers |
|---|---|---|
| Financial ledger | Double-entry journals, accounting periods, close/soft-adjustment approvals, per-user rights, reversible source trails, trial balance, income statement, financial position, aging, multi-tenant RLS prototypes | Deployed authenticated service, role/grant audit with non-superuser, close/consolidation sign-off, audited exports and actual finance data reconciliation |
| Internal invoicing and receipts | Invoice/payment subledger SQL and atomic Python persistence; replay request receipts 057, deferred commit seal 058, actual PostgreSQL two-connection race/rollback tests | Approved immutable source loader, mounted production HTTPS commands, separately approved tax pack; real bank reconciliation, settlement confirmation |
| Sales/purchases/warehouse | Source objects and transactional stock, reservations, partial receipts, dispatch, vendor goods invoice match, returns/quarantine, valued movements and GRNI prototypes | Authenticated operator write workflows, lot/serial/bins/expiry, proof-of-delivery evidence, shipment split/exceptions, verified landed costs and VAT/variance treatment |
| Commercial read API | HTTPS-only, signed, permission-scoped GET prototypes for sales/purchases/stock/returns; database-level tests | Real identity registry, real route mount, restricted reader role, API-to-page E2E, session recovery |
| Private finance commands | Existing soft-close / adjustment WSGI proof; new reference-only internal invoice/payment WSGI proof on review PR #44 | Server-only trusted immutable source implementation, HTTPS service + controlled deployment, service role grants, rate limits, audit, source lock and retry/restart acceptance |
| UI | Static bilingual RTL/LTR preview and private-view source components, navigation/brand checks | Real authenticated client wired to live source-backed APIs, accessibility/browser/mobile QA; **do not display fictional figures as business data** |
| Identity / tenant | Signed token validation, membership/role tests, isolated tenant DB CI, seven-day trial domain rules | Real IdP + MFA/recovery, email verification, tenant provisioning/restore in Oracle, operator access review, company lifecycle |
| SaaS billing | Domain and trial prototypes | Actual invoice/payment gateway, webhook signature, duplicate/reversal/chargeback handling, renewals and suspend/reactivate without granting paid status on unverified payment |
| Compliance | Accounting validation gates and internal VAT/NRV/test policies | Independent Saudi VAT + ZATCA clearance/reporting, QR/XML/credit note, SOCPA/IFRS review with real output, KSA payroll/WPS, other country packs |
| AI and reports | Prototype read-oriented insights; ONE PULSE/ASK ONE demo structures | Authorized live tenant data retrieval, traceable citations, controlled permissions, hallucination mitigation, monitored operations, no automatic high-risk posting |
| Sector packs | Static/demo layouts for medical/hospital/clinic/lab/pharmacy and design contracts; other verticals planned | Separate operational workflows, staffing/bed/medication/lot and patient safety for healthcare; BOM/WIP/production for manufacturing; technicians/SLA for service |
| Oracle and operations | Repository workflows/CI and legacy Oracle/preview instructions | Independent NEXT server/service deployment, actual TLS cert/route, secret vault, backup/restore rehearsal, metrics/alarms, staging sign-off and rollout |

## Ordered critical path

1. **Source and replay trust:** implement the actual immutable approved-document loader in backend code for invoices and payments, including historical snapshots after posting, current membership and role checks. Reject attempts to convert client JSON into ledger source values. Revalidate the source after any crash/restart.
2. **Database security:** isolate companies with real DB routing; migrate 057/058 on disposable/staging copies only; grant narrow table/sequence/function privileges to a dedicated NOLOGIN service role with separate secret-managed LOGIN. Validate PUBLIC/inherited grants, ownership, RLS, and that an unrelated service cannot mutate journals.
3. **End-to-end authenticated staging:** approved IdP token, TLS, host routing, source document approval, duplicate HTTP retries across worker restarts, simultaneous submissions, cancel/return/partial payment; verify journals, physical stock and documents remain consistent.
4. **Operational commercial write API:** order, receipt, reservation, delivery, return and refund evidence, source events and outbox retry. Retire or deny dangerous legacy/direct inventory procedures; implement lot/serial/expiry and effective warehouse authorization.
5. **Private bilingual UI:** link individual pages to authenticated APIs; real data, compact top navigation and module drop-downs; session/login recovery, legal-entity and branch selection, accessible phone/desktop behavior. Differentiate demo vs real modes.
6. **Localization/financial acceptance:** period close, price/FX/discounts/three-way match and IFRS/IAS2 reconciliations; legally reviewed VAT/ZATCA integration with formal test fixtures and negative paths; country-specific reports, print/export and bank reconciliation.
7. **SaaS operations:** email/MFA and isolated onboarding, subscription billing only after signed provider confirmation, invoices, auto-renewal, trial expiry, backups/restores, telemetry/alerting, incident handling, rollback rehearsals.
8. **Commercial pilot:** controlled actual customer/staff UAT, security and penetration review, load testing, independently reviewed financial/tax acceptance, written release sign-off. Only then decide on main merge and Oracle production deployment.
9. **Optional later packs:** payroll/HR/WPS, advanced CRM/POS/manufacturing/services, hospital inpatient/lab/pharmacy safely integrated, sector regulatory reviews and other country packs.

## Explicit no-go and evidence requirements

- PR #43: https://github.com/Midoalone85/onebusiness-site/pull/43 (057/058 and PostgreSQL replay acceptance).
- PR #44: https://github.com/Midoalone85/onebusiness-site/pull/44 (reference-only private HTTPS handoff, **no deployed source loader**).
- PR #41: https://github.com/Midoalone85/onebusiness-site/pull/41 (staging review, still draft and not equivalent to live staging).
- PR #38: https://github.com/Midoalone85/onebusiness-site/pull/38 (integration preservation/security gates).
- CI unit / disposable PG success does not prove a deployable, legally compliant ERP. No branch promotion, production migration, payment capture, ZATCA issuance or external bank action from this plan.
