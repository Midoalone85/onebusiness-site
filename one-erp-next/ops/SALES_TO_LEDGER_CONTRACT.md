# ONE ERP Next | Dispatched Sales → Customer Invoice → General Ledger

**Status:** draft, independent Python/PostgreSQL 14 prototype. NOT an Oracle
deployment, live invoice portal, ZATCA-compliant tax invoice or public API.

## Implemented and exercised in disposable CI

The source chain is:

1. Company-scoped confirmed sales order with immutable prices and lines.
2. `one_fulfill_commercial_line` or `one_dispatch_reserved_sale_line`
   writes a physical stock movement and increases the order line's
   `fulfilled_qty` transactionally.
3. A trusted backend prepares a `customer_invoice` with explicitly
   configured tax treatment and identified sales order line numbers.
4. `persist_dispatch_backed_sales_invoice` creates the internal invoice,
   posted double-entry GL and audit inside one outer transaction.
5. `one_link_sales_invoice` locks the confirmed sales order, validates its
   customer and currency, maps **every** invoice line to exactly one
   corresponding order line, checks the original unit price and caps
   total source-linked billed quantity to dispatched-but-not-yet-billed
   quantity. An immutable allocation row links order/line/invoice/GL.
6. ANY failure (wrong customer, price, order state, invoice quantity,
   duplicate invoice/source, invalid role) rolls back the whole transaction.

Source:

- `db/007_sales_invoice_sources.sql`
- `src/one_erp_core/sales_invoice_bridge.py`
- `tests/test_postgres_integration.py` (real PostgreSQL 14 integration)

## Important limitations

- Commercial orders currently support stockable goods only, not services,
  subscriptions, complex discounts/price overrides, composite kits, multi-
  currency conversions or multiple orders per customer invoice.
- Source mapping pairs invoice line N with a unique `order_line_numbers[N-1]`
  in the **same** sales order. Price must match the original unit price.
- Invoice and GL posting are *internal prototypes*. Actual legal invoice
  numbering, invoice print/PDF, national tax pack and ZATCA registration
  are NOT implemented by this linkage.
- Cost of goods sold (COGS), inventory valuation, returns, credit note
  source reconciliation and full customer-facing UI are still missing.
- Exact HTTP retries currently reject duplicate journal/invoice IDs instead
  of returning a safely replayable signed confirmation.
- No authenticated HTTP endpoint, no live ID provider, and no real dedicated
  tenant database deployment yet. `one.tenant_id` and `one.actor_id`
  PostgreSQL transaction variables are NOT authentication. CI grants ALL
  on tables to a throwaway role solely for security/testing convenience.
  They MUST NOT be copied to production.
- Source allocations are append-only; legal corrections need explicit
  compensating credit notes and reversal workflows after policy review.

## Next acceptance gates

1. Implement auth-to-database route with server-side identity verification,
   dedicated no-bypass tenant roles and locked-down SQL privileges.
2. Implement controlled `quotation -> sales order -> reservation -> pick
   -> ship -> invoice` API commands with audited event identities.
3. Inventory valuation and COGS separately from sales revenue;
   verify accounting period and tax compliance on the same business event.
4. Concurrency and load tests for simultaneous dispatch vs invoice,
   parallel invoices, failed transactions and retry/idempotency cases.
5. Compliant Saudi localization (VAT and ZATCA), printable documents,
   security audit, encrypted backups, restore drills and monitored HTTPS
   before any actual commercial pilot.

Never merge the draft into production, deploy to Oracle or expose sensitive
ledger records merely because isolated CI passes.
