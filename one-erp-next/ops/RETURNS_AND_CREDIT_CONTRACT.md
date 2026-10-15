# ONE ERP Next | Returns + source-backed customer credit contract
2026-10-15 | Standalone experimental Python / PostgreSQL 14 code, NOT deployed.

## 1. Stock return foundation
Source `db/010_stock_returns.sql`

- Original, tenant-bound immutable `sale_dispatch` can open a customer return.
- Original, tenant-bound immutable `purchase_receipt` can open a supplier
  return. Returning an already-created return event is not permitted.
- A source event's quantity is a hard ceiling across accepted completed
  returns and outstanding requested returns. Rejected quantities release the
  reservation for another **new** inspection request; source audit remains.
- Requests and approvals are immutable, admin-scoped in the SQL prototype,
  serialized by source order row locks, and cause append-only audit/outbox facts.
- Customer accepted return stock is put in an **explicit, active quarantine
  warehouse**, never automatically into the original sellable stock bucket.
  Only accepted quantity is physically recorded. A declined request has no
  physical stock movement. Fully or partly rejected units are NOT stocked.
- Supplier returns subtract stock from the receiving warehouse **only to the
  extent stock is currently unreserved**. A shortage or conflicting event
  ID rolls back the whole operation and all proposed audit/outbox facts.
- Vendor returns allow only full requested quantity or full rejection in this
  first policy. Actual supplier acceptance and freight shipping confirmation
  are NOT modeled.
- Refunds, replacements and accounting reversals do not happen when merely
  recording a stock return.

## 2. Return-backed customer credit
Source:
- `db/011_return_credit_links.sql`
- `src/one_erp_core/return_credit_bridge.py`
- PostgreSQL integration tests in `tests/test_postgres_integration.py`

A trusted accounting service can issue an INTERNAL `customer_credit` and
linked balanced reversal journal only if, in the SAME outer PostgreSQL
transaction, all conditions hold:

1. The return is a completed, accepted customer return tied to an original
   `sale_dispatch` event.
2. The original issued customer invoice contains a sales source allocation
   for the same order and order line; the source invoice is NOT arbitrary.
3. The credit note is issued and references that original invoice.
4. Only one inventory item is credited per credit document in this pilot.
   Source and credit account, unit price, tax category, tax rate and tax code
   must agree. Both original and credit lines have no line discount.
5. The new credited quantity is <= (return accepted qty minus prior
   return-linked credits) AND <= (original invoice billed qty minus other
   return-linked credits on that invoice/order line).
6. The existing invoice subledger separately bounds monetary credits to
   the original invoice's remaining gross value.
7. Failure rolls back credit document, GL posting, source link, audits and
   outbox facts together. Immutable source allocation holds the lineage.
8. Different physical-stock and accounting rights are validated independently
   in their source transaction policies.

**No automatic cash/bank/card refund is made.** No legally compliant
credit-note numbering or ZATCA tax invoice issuance is implied. Existing
country pack/tax validations remain mandatory before commercial activation.

## 3. What is *not* finished

- Warehouse quarantine release workflow (QA approve, dispose, refurbish,
  repair, restock) and batch/serial/expiry tracking are still to be built.
- Customer credit service supports only single-item stock returns without
  discounts or tax overrides. Multi-line and service credits require a
  separate audited policy.
- Supplier credit notes/vendor bills are NOT tied to returned receipts yet;
  complete purchase order -> accepted goods -> vendor bill 3-way match first.
- Inventory value / weighted-average cost layers, COGS reversal, VAT recoveries,
  currency remeasurement and exact inventory/GL reconciliation remain open.
- A digitally verifiable proof-of-delivery, shipment carrier API, delivery
  rejection and pickup proofs do not exist in this backend yet.
- Signed identity-provider validation, operator authorization, tenant
  provisioning with independent physical databases, least-privilege production
  database roles, authenticated public/private API endpoints, encrypted
  backup/restore, key management, HTTPS and security penetration tests remain
  non-negotiable release gates.
- CI grants are deliberately broad against a disposable PostgreSQL 14 database,
  and transaction-local `one.tenant_id`, `one.actor_id`,
  `one.return_context` and `one.commerce_context` are NOT authentication.
  Never expose these routines to browsers or use these grants in production.
- Preview UX is fictional and disconnected from this backend. Nothing in this
  branch changes Oracle customer data, real stock, money or the published UI.

## 4. Integration acceptance criteria

- A duplicate return UUID, wrong actor/tenant, over-return and already
  processed return must fail without stock mutation.
- Competing requests against the same origin movement cannot overcommit.
- Supplier return cannot consume stock reserved for customer orders.
- All accepted customer returns are visible in quarantined stock, never
  as original sellable units.
- A customer credit can be issued only against already accepted return
  quantity and source-invoiced units; excess/wrong-price/wrong-tax/unknown
  source credit must roll back all finance records.
- Outbox is an append-only record of atomic source facts, not an active
  message worker. External consumers/retries/notifications are not built yet.
