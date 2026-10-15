# ONE ERP Next | Remaining gaps and dependency-driven delivery gates
Baseline 2026-10-15. No dates, arbitrary percent-complete or deployment claims.
This project is a STANDALONE business platform, not an Odoo module.

## Gate A | Commercial physical flows (prototype largely covered)
Already present in disposable PostgreSQL CI: purchase receipt QC, sales stock
reservations, picking/packing, dispatched physical stock, a delivery-proof
**reference only**, return-to-quarantine, supplier returns from unreserved
stock, QA restock/disposal, internal return-backed customer credits and an
append-only same-transaction operational outbox + cross-module RMA timeline.

Still required before real customer use:
- Verified evidence capture and acceptance for carrier handoff and proof of
  delivery; cancellation, split/multi-line shipment, lost goods, returns from
  delivered vs disputed orders, warehouse transfers and bin/lot/serial-level
  stock including expiration, recall and quarantine ownership per RMA.
- Supply-chain exception policy: partial acceptance, repair, exchange, refund,
  warranty, claim rejected, vendor RMA and reverse physical logistics.
- Tests for serialized inventory and unexpected duplicate/failed HTTP retries
  including restart, concurrency, partial fulfilment and recovery.
- Quarantine disposition financial cost/COGS consequences in source-linked GL.

## Gate B | Valuation and financial correctness (NOT READY)
- True tenant-bound cost layers and weighted-average/FIFO method elections,
  adjustments, landed cost, opening stock, revaluations, disposal and returns.
  Only a Python COGS *proposal* exists; nothing is posted as real inventory
  value or audited real COGS.
- Three-way match: purchase order vs inspected receipt vs vendor bill. Support
  partial receipts, foreign currency, price and quantity tolerances, invoice
  duplicates, approval matrices and accrued goods-received-not-invoiced.
- Reconcile dispatch/credit/receipts with GL, cash/bank, discounts, VAT
  treatments, supplier credit and credit note reversing COGS and inventory.
- Real period close, branch/company consolidation, debt ageing, reconciled
  cash flow, profitability by item/order/warehouse, and robust report exports.

## Gate C | Operating product and safety (NOT READY)
- Authenticated HTTPS API with trusted identity verification and tenant
  routing to isolated tenant databases. SQL GUCs such as `one.tenant_id`
  can be forged and must not be used as proof of identity.
- Production least-privilege DB roles and migrations, approvals, immutable
  operator audit, durable idempotency, retry workers and external outbox
  consumers. **CI grants must never be copied to production**.
- Live RTL/LTR application workspaces, contextual top bar and compact
  vertical dropdown menus; real stock/status/finance data from the API.
  The existing GitHub preview uses fictional rows and is not the product.
- Identity recovery, full backups with successful restore drills, penetration
  tests, capacity/load tests, secret and key management, incident monitoring,
  actual TLS verification, and proper CI -> staging -> production promotion.

## Gate D | Compliance and commercialization (NOT READY)
- KSA compliant VAT, ZATCA integration to the formally required phase for
  each obligated tenant; invoice/credit note signed numbering, QR/schema and
  safe retry/failure policy; other countries have separate tax packs.
- Multi-tenant subscription lifecycle, payment capture + invoicing +
  automatic renewal, checkout verification, trial provisioning and accurate
  subscription state; no marking paid without actual settlement.
- End-user onboarding, help, role permissions, test fixtures, system settings,
  tenant privacy data retention, commercial release and staged pilot signoff.

## Sector packs: AFTER shared platform contracts
- Trading/distribution: finish Gates A-D, including customer/vendor returns.
- Healthcare: patient and consent safety, hospital beds/wards, pharmacy
  lot/expiry and medication custody, lab/radiology, clinical and billing
  interfaces. No automatic use of commercial return rules for prescription
  drugs without explicit clinical workflow and approved QA.
- Manufacturing: BOM, routing, work orders, batch consumption, WIP, costing.
- Service/maintenance: field work orders, spare parts, technicians, SLAs,
  warranty and service contracts.

## Near-term next coding order
1. Immutable inventory valuation source and its purchasing receipts, including
   accounting mapping and scenario-based independent tests.
2. Post COGS atomically from actual shipment and accepted QA disposition,
   reversing it on eligible customer returns with cost-layer provenance.
3. Implement buyer/supplier three-way match + vendor credits, preserving
   GRNI, tolerance and cash/GL audit.
4. Build authenticated backend APIs against a dedicated staging tenant, and
   connect compact bilingual application workspaces only after permission
   checks and tenant isolation verification.
5. Add approved country tax packs and production readiness gates.
Do not merge or deploy a prototype just because unit/CI tests are green.
