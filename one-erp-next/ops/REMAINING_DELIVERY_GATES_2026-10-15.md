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

## Gate B | Valuation and financial correctness (PROTOTYPE; NOT READY)
- **NEW CI-only proof:** `015_stock_valuation_ledger.sql` records a tenant-
  and warehouse/product-scoped single-currency stored weighted-average
  valuation; original purchase receipts post Inventory Dr / GRNI Cr, and
  original sales dispatches post COGS Dr / Inventory Cr, with one linked
  immutable posted journal per valued stock event.
- **NEW CI-only proof:** `016_goods_vendor_bill_match.sql` and
  `vendor_goods_bridge.py` match PO supplier, source valued receipt,
  original prices and unbilled quantities BEFORE issuing an internal vendor
  invoice, then post GRNI Dr / AP Cr in one transaction. The v1 route is
  **tax-free only, no discounts, no tolerances and one PO per bill**.
- **NEW CI-only readout:** `017_inventory_finance_reconciliation.sql` compares
  physical available/on-hand stock to valued quantity, book value and
  outstanding GRNI, flagging unvalued physical movements. A static
  bilingual preview page exists but is NOT connected.
- STILL REQUIRED: atomically commit physical stock and valuation in the same
  authenticated backend operation. Current separate transaction stages are
  safe only for this isolated, fail-closed demonstration.
- STILL REQUIRED: true lot/serial-level inventory ownership, landed cost,
  opening balances, FX, approved cost corrections, returns/QA/disposal COGS
  reversal, three-way match with tax and supplier credits, country rules,
  period-end account reconciliation, bank/cash and management reporting.
- Finish real period close, branch/legal entity consolidation, debt ageing,
  profitability by item/order/warehouse and source-traceable exports.

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

## Near-term next coding order (after 015-017 prototypes)
1. Unify purchase/sale physical movement and cost journal into one trusted
   operation and add restart, retry and concurrent consumer tests.
2. Add lot/serial-owned warehouse balances, quarantine and approved physical
   returns; post proper reversal/disposal valuation, not only stock units.
3. Upgrade supplier goods billing beyond zero tax: verified VAT input tax,
   cost variances, GRNI adjustments and return-backed supplier credits.
4. Connect real authenticated APIs and least-privilege database accounts to
   RTL/LTR separate workspaces and live ONE PULSE data.
5. Close accounting reconciliation, approved VAT/ZATCA compliance, billing,
   security, backup restoration and staging validation.
Do not merge or deploy a prototype just because isolated CI is green.
