# ONE ERP Next | Cost of Goods Sold (COGS) staging gate

**State:** pure Python policy and unit tests only. No actual cost ledger, VAT,
physical valuation journal posting or Oracle production change exists here.

## Proven draft policy

`stock_costing.py` accepts a trusted, tenant/warehouse/product/currency-matched
`CostSnapshot` with pre-dispatch on-hand quantity and prior inventory value,
along with an immutable stock sale dispatch event. It proposes:

- COGS = prior stock value * dispatched quantity / pre-dispatch stock on hand,
  rounded half-up in the configured currency.
- Debit COGS expense and credit inventory asset in a balanced **draft**
  `JournalEntry` traceable via `cogs:<tenant>:<stock_event>`.
- Remaining stock quantity and remaining inventory book value. If the full
  remaining quantity is dispatched, consume the entire remaining book value.

Non-finite quantities, wrong currency, tenant/location mismatch, absent
inventory expense accounts, closed periods, zero-value/round-to-zero costs,
wrong event kind and overstated dispatches are rejected.

**This is a mathematical proposal, not a GL posting or inventory valuation.**

## Required infrastructure before a live financial posting

1. Store a transactionally consistent inventory cost ledger per tenant,
   warehouse, product, currency and valued receipt. Define methods and
   assumptions for goods in transit, landed cost, opening stock, supplier
   returns and adjustments.
2. Under row locks, bind exactly one immutable `sale_dispatch` movement
   to its valuation layer and ensure source-event idempotency at the DB level.
   Prohibit user-supplied costs/warehouse balances in real APIs.
3. Verify the valuation policy and accounting mapping per legal entity,
   period and local standards. Handle freight, discounts, returns and
   very-small/zero-cost items explicitly; never silently drop COGS.
4. Persist the stock valuation entry and balanced COGS journal atomically,
   not via an unverified detached proposal.
5. Reconcile inventory value, COGS, revenue, sales returns, stock adjustments
   and general ledger. Add concurrency/recovery tests and auditable exports.
6. Test security, real auth roles, separated tenant databases and backup/
   restore before commercial activation.

Remaining accounting controls must be completed independently of the
fictional UI preview and draft source-linked sales invoicing.
