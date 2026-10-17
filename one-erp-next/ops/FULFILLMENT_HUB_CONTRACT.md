# ONE ERP Next | Fulfillment Hub | Engineering acceptance contract
Date: 2026-10-15

## Intended product
ONE Fulfillment Hub is the common physical-operations engine for trading,
distribution, wholesale, services with stock, and later sector-specific packs.
It belongs to ONE ERP Next's **original standalone** Python/PostgreSQL stack,
not the legacy Odoo server. The prototype is not deployed to Oracle.

The commercial modules expose different workspaces against the **same**
order/stock/financial source IDs:
- Sales -> delivery orders, shipment status, proof reference, invoices.
- Purchasing -> receiving inspection, accepted and rejected quantities, vendor bills later.
- Inventory -> reservations, pick/pack, outgoing and incoming movements, stock ledger.
- Accounting -> source-linked customer invoice, journal, audit, future valuation.
- ONE PULSE / ONE TRACE -> eventual materialized read models, not implemented yet.

## Outbound: available today in disposable PostgreSQL CI
1. Confirm a sales order and reserve goods at warehouse/product/line level.
2. `one_create_delivery` allocates part of that line's reserved quantity
   to a **single-line delivery job**. Multiple jobs can serve one order line.
   Two simultaneous jobs cannot allocate the same reserved physical quantity.
3. Stage sequence is enforced:
   `draft -> picked -> packed -> dispatched -> delivered`.
   Skipping/repeating a stage is refused. Each change adds an immutable audit.
4. Dispatch requires a fresh movement UUID and calls the existing
   `one_dispatch_reserved_sale_line` inside the SAME SQL transaction. Stock
   on hand and reserved both decrease, source fulfilled increases, immutable
   movement is written, and a delivery job points to the event.
5. Delivery confirmation stores a limited proof **reference**, not signed
   proof or legal evidence. Proof may be recorded only AFTER dispatch.
6. Existing direct stock-release/reserved-dispatch functions cannot consume
   stock allocated to a delivery job still at draft/picked/packed.
7. Sale invoices are separately restricted to total physically dispatched
   but previously unbilled quantities; their internal invoice/journal/source
   mapping commits or rolls back atomically in the finance prototype.

Shipping is the current inventory outflow point. Goods **in transit**,
legal revenue recognition timing, ownership-transfer terms, carrier API
and actual electronic signatures require later domain design/review.

## Inbound: available today in disposable PostgreSQL CI
1. Confirm a purchase order. `one_start_receiving_inspection` locks
   the order and line, and reserves an *inspection quantity* from the still
   outstanding purchased quantity.
2. `one_complete_receiving_inspection` requires
   `accepted_qty + rejected_qty = expected_qty`.
3. Only accepted units cause `one_fulfill_commercial_line` and an immutable
   `purchase_receipt` stock movement; rejected units create no stock.
4. An entirely rejected inspection closes its inspection without receiving
   any stock, leaving the purchase quantity outstanding for a replacement.
5. Other receipt attempts cannot consume quantities committed to pending
   inspections. One inspection cannot be completed twice.
6. All inspections, completions and movements are audit-traceable.

No QC checklist/attachments, vendor RMA, purchase-to-AP three-way matching,
serial/lot/expiry, bin/putaway, quarantine virtual warehouse or actual goods
inspection mobile UI is implemented yet.

## Cross-module facts (NO pub/sub delivery yet)
`009_operation_outbox.sql` creates append-only per-tenant facts for:
- `stock.purchase_received`, `stock.sale_dispatched`;
- `delivery.created`, `delivery.picked`, `delivery.packed`,
  `delivery.dispatched`, `delivery.delivered`;
- `receiving.inspection_started`, `receiving.inspection_completed`;
- `finance.invoice_issued`, `finance.journal_posted`.

Each event contains an opaque source UUID, event type, tenant UUID and a
monotonic PostgreSQL `stream_position`. No customer names, medical data,
tokens, emails or other sensitive payload are placed in the queue. Producers
write records in the same database transaction as their business mutation.
A failed transaction has NO committed business event. Each
`(tenant,event_type,source_id)` is unique.

**Not yet implemented:** an authenticated event consumer, permission-filtered
UI notification service, outbox retries/checkpoints, compensating events,
subscription handling, dashboards, or external webhooks. A future consumer
must checkpoint per tenant and process events **idempotently**. Sequence
gaps and cross-transaction visibility may occur; do not call this an
exactly-once message bus.

## Non-negotiable operational and accounting rules
- Keep `on_hand`, `reserved`, `line_reserved`, `fulfilled`, billed
  source quantities and receipt inspection quantities consistent.
- Every physical stock change must have a source and immutable movement.
- Revenue from invoice ≠ gross profit. Real inventory valuation and COGS
  require a trusted cost ledger and balanced posted journals.
- Delivery and receiving are **not** tax e-invoice creation. ZATCA/returns,
  purchase VAT recovery and legal ownership must be explicitly approved.
- Do not delete financial or movement history. Use accepted reversals,
  compensating events, returns and credit notes after implementing policies.

## Security and release blockers
- All CI roles are disposable and **intentionally overprivileged**.
  PostgreSQL `one.tenant_id`, `one.actor_id`,
  `one.delivery_context` and `one.receiving_context` may be set
  by any connected SQL user; these are NOT authentication or safe delegation.
- NO production client may connect directly to this schema. Build the
  external trusted IdP/MFA + company entitlement routing, independent
  dedicated tenant databases and narrow backend roles before exposure.
- Enforce controlled migrations, backup/restore, HTTPS, TLS and secrets
  isolation, monitoring, payload minimization, operator segregation,
  rollback/idempotency, penetration/load tests and security review.
- Current `one_delivery_jobs` supports **one sales order line per job**;
  multi-line shipments/packing, route planning, bins, carrier tracking,
  signed proof, returns and exception states are future modules.
- Direct raw stock APIs inherited from the prototype still exist for
  previously unallocated quantities. Live policy must govern or retire them
  before making picking a mandatory warehouse workflow.
- The preview UI is fictional, works only with static sample rows, and
  does not connect to these backend routines or customer databases.

## Next independently testable vertical slices
1. Lot/serial/expiry and bin-level stock management, inbound quarantine,
   damage/back-order and partial receipts with photo evidence.
2. Pick-list and packing lines, operator roles, barcode scanning, multi-line
   shipment consolidation, in-transit handoff, real proof capture/verification.
3. Cancellable/reschedulable jobs, lost delivery, customer return, vendor
   return and audited accounting reversal with compensating events.
4. Durable inventory valuation, cost of goods sold, three-way procurement
   matching, real finance/tax checks and source-level reconciliation.
5. Authenticated REST/application interface and live Arabic/English separate
   workspaces; only then real tenant pilot with external security signoff.
