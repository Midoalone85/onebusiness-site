# ONE ERP NEXT | 044 Inventory-to-finance period close gate

Status: CI-only PostgreSQL 14 pilot, 15 October 2026. Not merged,
not connected to the static preview, and NOT deployed to Oracle.

## Change
- `db/044_inventory_period_close_gate.sql` extends period close 043
  to require **both** its refund / draft-ledger preflight and an independent
  current-position inventory preflight.
- Inventory preflight uses the existing source-backed inventory/GRNI
  reconciliation per (warehouse, product). It rejects positions missing
  a physical balance or value position, physical vs book quantity differences,
  legacy unvalued movements and the source report's unreconciled GRNI state.
- Positions present **only** in the valuation table are counted, not dropped.
- The result is an immutable, company- and period-bound
  `one_period_inventory_close_evidence` snapshot, inserted alongside the
  existing refund close evidence and close audit in the same transaction.
  On failure, **none** of the three audit records or the period close persist.
- Only active company admins in the matching SQL tenant/actor scope can
  execute the preflight or close. Functions are REVOKEd from PUBLIC; CI uses
  an expressly broad disposable test role with an explicit grant.
- Amounts in mixed currencies are **not summed** by this migration.
  Outstanding, correctly recognized GRNI is not automatically an error.

## Important limitations, not release-ready
1. The inventory check is an **as-of-now** position check; it is NOT an
   accurate historical as-of-period inventory rollforward. Future events or
   backdating across a closed period need separate event-date, posting-period,
   cycle-count and cut-off controls.
2. The gate does not prove physical custody, reliable purchasing, legal
   supplier billing, valuation policies, tax, signed documents, or actual
   GAAP / IFRS / SOCPA compliance.
3. During simultaneous transactions there is no proven global
   warehouse-to-period serializable close boundary; all production movement
   entry points must consistently lock the relevant period and stock
   positions. Existing old unvalued stock functions must be denied.
4. PostgreSQL caller-configurable `one.tenant_id` and `one.actor_id` are
   **not authentication**. Production requires audited service-only roles,
   real IdP/MFA, physically isolated tenant DB, encryption/secrets and
   no direct SQL for customers.
5. Latest branch CI needs a clean run on **both Python 3.11 and 3.13**
   and disposable PostgreSQL 14. The tests alone do not approve release.

## Next milestones
- 045: per-period stock and valuation roll-forward with verified
  period/cut-off ownership, including transfers, approved inventory counts,
  source-ledger and distinct currency subregisters.
- Independently verified close evidence and read-only drill-down in
  authenticated ONE PULSE, not fabricated preview numbers.
- Bank statement provenance / settled refund evidence, tenant-specific
  authorized HTTP workflows, ZATCA and KSA legal financial reporting pack.
