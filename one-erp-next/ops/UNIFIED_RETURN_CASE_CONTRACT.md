# ONE ERP Next | ONE Returns Hub | unified inventory + financial case

Date: 2026-10-15  
Status: **independent experimental PostgreSQL 14 / Python prototype, not deployed to Oracle**.

## Business principle

A return is one business case (RMA) and two deliberately independent, audited
operational tracks that must be reconciled:

| Track | Outcome | Authorization | Must NOT automatically do |
| --- | --- | --- | --- |
| Stock / quality | Request, inspect, accept, quarantine, restock, discard, vendor return | Warehouse / QC | Issue cash refund, credit note, or secretly change GL |
| Finance | Original invoice check, internal credit-note/posted journal, future settlement | Finance | Change stock or mark a QA inspection as accepted |

The same immutable return ID joins both tracks. The return case may be fully
resolved physically while only partly credited financially. Conversely,
financial credit quantity may be constrained by accepted stock and the quantity
originally billed. **No customer credit is issued merely on return request.**

## Implemented in disposable PostgreSQL CI

**Already existed before this step**

- `010_stock_returns.sql`: accepted customer goods enter a separate
  quarantine warehouse, rejected goods never become available inventory;
  supplier returns can remove only unreserved physical stock, with original
  source movement caps and immutable events.
- `011_return_credit_links.sql` and `return_credit_bridge.py`: internally
  issued customer credit and posted GL atomically tie to the accepted return,
  original invoice, exact price/tax attributes and credited quantity caps.
  No real refund/payment, legal tax invoice or ZATCA submission.

**New this step: `012_unified_return_operations.sql`**

- `one_return_dispositions`: immutable, tenant-scoped QA outcome records for
  stock released to a saleable warehouse (`restock`) or physically discarded
  (`dispose`). A restock writes separate immutable stock-out and stock-in
  events; disposal writes an immutable stock-out event. Both update stock
  balances in ONE SQL transaction with conditional checks and event outbox
  facts. Repeated IDs, inactive/quarantine restock destinations, cross-tenant
  access and quantities exceeding accepted goods must fail.
- `one_resolve_return_quarantine(...)`: no auto-credit and no stock
  re-availability until an explicit warehouse-admin disposition; only
  previously **accepted customer returns** qualify. Available stock in the
  quarantine bucket is rechecked and locked at write time.
- `one_return_case_overview(tenant,return,actor)`: read-only,
  `SECURITY INVOKER` function for PostgreSQL 14, so it does **not** expose
  cross-tenant rows through an owner-rights view. It returns physical case
  state and financial case state in one response, plus requested/accepted,
  QA-dispositioned, quarantine-remaining and credit quantities and original
  order reference. Vendor return cases explicitly flag invoice matching
  as **not implemented**.

Independent frontend branch offers a fictional/static bilingual
`return_case` workspace in Sales, Purchasing and Inventory, with a narrow
side-by-side warehouse/finance state strip and links to related modules.
It does not connect to these services and cannot perform real actions.

## Example customer RMA

1. Receive 2 items from an earlier sale and accept 2 during QC.
2. Warehouse shows **2 in quarantine**, not saleable.
3. QA restocks 1 item and disposes 1 item. Warehouse shows **0 in
   quarantine**, **1 added to saleable**, and both decisions are audited.
4. Finance issues a one-unit internal credit linked to the original invoice:
   return case shows **physically resolved** but **partially credited**.
5. Finance may credit another eligible accepted unit in a separately
   authorized transaction. This does not move stock a second time.

The physical outcome is not synonymous with financial settlement. For example,
an exchange may move stock without a cash refund; a repair may keep goods in
a service area; a sale not yet invoiced may require no credit note. Those paths
are **design targets, not coded functions** in this prototype.

## Essential controls and limitations

- Current physical return QA is **one source stock movement / one stockable
  order line at a time**. Multi-line RMA consolidation is not implemented.
- Quarantine balances are aggregated by warehouse and product, not lot/serial
  or an individual RMA-owned container. Individual case **quantity** is capped,
  but future lot/bin/serial ownership is mandatory for high-assurance physical
  traceability (healthcare, pharma, warranty or recall items).
- `one_return_case_overview` is an operational overview and does not
  determine legal refund entitlement or country-specific revenue timing. The
  `not_invoiced` / `credit_review_pending` conditions use source order-line
  totals as supporting signals, not a substitute for exact invoice allocation
  reconciliation on each movement.
- No cost-layer transfer, salvage/scrap accounting, reverse COGS, warranty
  provision, invoice tax compliance, inventory valuation, bank/payment
  refund or supplier-credit matching is automatically posted on QA disposition.
  These are release blockers.
- No automated quarantine → available conversion; it requires explicit
  trusted operator action. No serial/lot, quality images, barcode workflow,
  repairs, replacement shipping, approvals matrix or signatures yet.
- Session parameters `one.tenant_id`, `one.actor_id`,
  `one.commerce_context` are NOT authentication. CI uses intentionally broad
  grants in a throwaway PostgreSQL database and must never be copied to
  production. Build trusted identity/role verification, separate tenant
  databases, least privilege, encrypted backups, independent security review,
  end-to-end integration tests, HTTPS, and tax/ZATCA compliance before live use.
- Production Oracle, existing customer invoices, stocks and journals are
  unchanged; neither branch has been merged or deployed.

## Next milestone

Implement per-return/lot-owned quarantine buckets, cross-warehouse transfer
service, real GL inventory valuation and COGS disposal/reversal, purchase
three-way bill match, separately approved credit/refund/exchange paths and
authenticated live return-case UI. Add stronger concurrency tests and
acceptance proof from real warehouse users.
