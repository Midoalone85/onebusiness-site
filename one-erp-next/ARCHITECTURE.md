# ONE ERP Next: independent foundation

**Status:** early, tested core rules, not a live service. Neither email, login, Oracle nor payment gateways are connected.

> **2026-10-18 architecture addendum:** This file records the initial foundation, not
> the full current feature-branch test count. Draft financial code, private UI
> and integration gates have since been developed, but none proves a live
> commercial ERP. The current scale decision is one **logical, modular
> platform core**, a deliberately controlled initial deployment, and **NO
> paid/automatic horizontal autoscaling until after commercial launch**.
> Preserve tenant/identity, ledger idempotency, financial consistency and
> audit invariants so future replicas can be introduced after real
> concurrency/restore/security/load tests. See
> [ADR_SINGLE_CORE_SCALE_LATER_20261018.md](ops/ADR_SINGLE_CORE_SCALE_LATER_20261018.md).
> Neither this ADR nor the added router-boundary unit test changes production
> configuration, activates replicas, deploys Oracle, or replaces independent
> production acceptance.


- Core owns tenant identities, authorization contracts, accounting invariants and trial states.
- Versioned country packs will supply regional tax, forms, currency, legal rules and official integrations without changing core.
- Sector packs (medical, pharmacy, laboratory, industrial, maintenance, trade and more) supply sector features via stable contracts. They must not bypass tenant or ledger rules.
- Arabic (RTL) and English (LTR) via external translation resources.
- Open-source infrastructure may be used under its license; original ONE ERP business logic remains independent of Odoo.
- Admin activation will use an expiring single-use email invite and MFA, **never passwords hardcoded or posted to GitHub**.
- Email verification must be validated server-side before onboarding and tenant provisioning.
- A seven-day trial must be measured with UTC timestamps server-side. Upgrading to paid requires confirmed payment from an authenticated provider webhook.
- Deny cross-tenant access by default; production must also enforce tenant restrictions in PostgreSQL queries, background tasks and reports.

## Implemented in this branch

Pure-Python domain logic for trial duration, expiry boundary, timezone validation, per-tenant membership and role access. Ten local standard-library unit tests passed prior to upload. Those tests are not evidence of live signup, secure deployed authentication, or database-level isolation.

## Next

Create a separate Django/PostgreSQL service; implement verified email onboarding, MFA, audited login, signup protection, transactional provisioning, tenant-bound ORM access, integration tests and billing transitions. Later add Saudi Arabia accounting localizations, sectors and additional countries.

A user request to deepen ChatGPT's general scientific reasoning is **separate** from ONE ERP; no global knowledge module is included in this change.
