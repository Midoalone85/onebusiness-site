# ONE ERP Next | Stored inventory value, COGS, receipt-to-vendor bill, ONE PULSE
2026-10-15. **Experimental PostgreSQL 14, NOT deployed or a customer-facing API.**

## 1. Posting contracts actually implemented

The standalone goods accounting chain now consists of three linked, independent
and fully auditable data sources, under the same company/tenant:

**PO confirmed → physical goods receipt → inventory value posting**

- A purchase receipt creates a real physical stock event and increments warehouse
  `on_hand`. Its `one_value_physical_stock_event` then reads the immutable
  order/line price and receipt, verifies company-admin rights, locks the source
  order, stock bucket and valuation balance, and creates a paired posting.
- For 5 units at SAR 12.50, inventory gets Dr 62.50, GRNI liability Cr 62.50.
  `one_stock_value_positions` persists warehouse/product quantity, book value,
  and single source currency; the immutable `one_stock_value_events` binds
  stock movement to exactly one posted journal.
- This cost is the explicit PO **unit purchase price** in the first-cut pilot,
  not an approved landed-cost or weighted-average source for later adjustments.

**Value-qualified receipt → supplier goods bill → AP / GRNI**

- `one_link_goods_vendor_bill` locks the confirmed PO and matches every vendor
  bill line to a unique source PO line. It checks supplier, currency, unit price,
  zero tax, no discounts, and caps the sum of partial vendor bill quantities to
  **physically received and financially valued stock only**.
- `persist_valued_goods_vendor_bill` creates the draft internal invoice and
  balanced journal, inserts immutable goods allocations **before issuing** the
  vendor bill, then invokes the database issuer in the same outer transaction.
- The updated `one_issue_invoice` independently permits a vendor bill to debit
  a liability account (GRNI) only for invoice lines carrying already-validated
  source allocations. An unlinked ordinary vendor bill still requires its
  original expense account type.
- For 3 matched units at SAR 12.50, GRNI liability Dr 37.50; AP Cr 37.50.
  The stock balance does not change again merely because the supplier billed.
- Parallel competing bills and overbilled quantities are serialized by the
  PO order lock. Any rejected match rolls back invoice, posted journal,
  invoice audit and goods allocations together.

**Sale dispatch → inventory valuation / COGS**

- A physical sale dispatch reduces `on_hand`. The valued movement records
  weighted-average cost from authoritative stored pre-dispatch quantity/value,
  Debits COGS expense, Credits inventory asset, and appends a cost source link.
- For 5 items worth SAR 62.50, dispatching 2 posts Dr COGS 25.00 / Cr
  inventory 25.00, leaving 3 items worth SAR 37.50.
- Purchasing 2 items for SAR 25 and 3 for SAR 60 before selling 1 generates
  COGS SAR 17 and remaining inventory SAR 68, with precision validation.
- A completely depleted bucket must end with exactly zero stock and book
  value. Unsupported tiny/zero-valued accounting is rejected rather than
  silently rounded away.

## 2. ONE PULSE / finance-stock source readout

`one_inventory_finance_reconciliation` is a tenant- and membership-filtered
PostgreSQL 14 **read function**, not a deployed dashboard. Given a warehouse
and a product it reports:

- physical on-hand, reserved and available quantities;
- last posted valuation quantity and inventory book value;
- count of physical stock events without stored valuation;
- total valued purchase receipts, supplier-billed matched cost and
  outstanding goods-received-not-invoiced (GRNI);
- a fail-closed reconciliation warning if physical units != valued units,
  any physical event is unvalued, or billed receipts exceed their
  valuation source.

The separate GitHub preview pages `inventory/reconciliation` and
`purchasing/three_way` show illustrative fictional values. They do **not**
read these APIs or reveal customer data. The demonstrated values are examples,
not externally attested balances.

## 3. Boundary conditions / release blockers

**This first-cut ledger is deliberately narrow.**

- It only values `purchase_receipt` and `sale_dispatch` movements, exactly
  in step with their physical bucket. It **rejects** stale valuations if
  other unvalued movements have already altered the same bucket.
- **NEW 018 atomic operation functions** now combine physical goods receipts,
  physical sales dispatch, reserved dispatch and picked/packed delivery dispatch
  with stored valuation and a posted GL journal **in the SAME PostgreSQL
  transaction**. A costing/period/account failure rolls back the stock
  movement, delivery stage, audit and journal together.
- Legacy individual physical movement and valuation functions still exist;
  their direct use allows temporarily unvalued stock. Production must retire
  or restrict them and expose ONLY authenticated application commands for
  movement+valuation with narrowly-scoped DB permissions. No public API is
  implemented yet.
- Purchase valuation is derived from confirmed PO unit price; there is no
  supplier freight, tariff, landed-cost allocation, retrospective price
  variance, currency conversion, discounts, multi-price tolerances or
  approved manual opening valuation. These require separate inventory
  accounting and approval.
- Vendor goods matching currently supports **zero tax and no discounts only**
  and one PO per bill (multi-line items are allowed within that single PO).
  Actual KSA input VAT 15% / ZATCA compliant invoice handling MUST be added
  under a verified country pack before vendor goods invoicing is commercial.
- No supplier credit/rebate, vendor return GRNI/AP reconciliation, accrual
  reversals, customer return reverse COGS, quarantine value transfer,
  disposal writeoff, rework, warranty or exchanges currently post the
  complete financial cost chain.
- Other original invoice modes (expenses, customer invoices, services and
  their credits) are separate; a supplier goods bill is NOT allowed to
  bypass GRNI matching into arbitrary GL expense.
- The reporting function does not attest GAAP/IFRS/KSA compliance,
  period-end reconciliation, verified payment settlement, validated
  country tax rate, landed-cost basis or bank balances.
- CI runtime role intentionally has broad grants in a throwaway DB; SQL
  `one.tenant_id`, `one.actor_id`, `one.valuation_context` and transaction
  context variables are NOT trustworthy authentication. A real operational
  backend must choose isolated company DB via verified identity, own
  narrowly-privileged roles, enforce immutable source transitions, protect
  secrets and prove restore / incident / penetration scenarios.
- Outbox events carry immutable references, but an actual notification worker,
  UI data delivery service, retries/idempotency and audited approvals are
  still unimplemented. No transaction here moves real money.
- There was **NO Oracle deployment**, NO merge to main, NO modification
  to existing customer stock or invoices during this change.

## 4. Remaining dependency sequence

1. Bring the approved cost basis and posting into a single trusted
   receiving/dispatching API transaction. Add adversarial concurrent and
   crash/retry/recovery testing.
2. Lot/serial/expiry/bin quarantine provenance; reverse actual cost on
   accepted returns, treat warranty/disposal with proper capital/expense
   journals and explicit approvals.
3. Supplier credits, tolerances and complete three-way matching with
   country tax packs (including nonrecoverable tax capitalisation), FX
   and transport/landed costs.
4. Real inventory/AP/GRNI bank/GL tie-out by accounting period and legal
   entity, plus authenticated/exportable ONE PULSE reports with source
   drill-down and alerts.
5. Integrate bilingual independent page routes with verified APIs,
   least-privilege company roles, MFA/tenant isolation and signed approvals.
6. Pass security, VAT/ZATCA, backup/recovery, performance/load,
   trial-to-paid billing and commercial staging acceptance gates before
   enabling any live company or sector-specific pack.

## October 15 extra acceptance gate: atomic physical movement

`db/018_atomic_stock_accounting.sql` includes four composable business
operations: `one_receive_and_value_goods`, `one_ship_and_value_goods`,
`one_dispatch_reserved_and_value_goods`, and
`one_dispatch_delivery_and_value`. All operate within one SQL function call
and PostgreSQL transaction, calling the already validated stock and cost
functions; one failure aborts all changes. Integration tests exercise successful
unit/cost/journal consistency, invalid GL accounts, invalid accounting periods,
reservation rollbacks and delivery stage+cost atomicity. No live Oracle
deployment, no ZATCA approval or authorized REST interface is claimed.

**Latest isolated CI:** 318 tests passed on each Python 3.11 and 3.13 with
PostgreSQL 14: https://github.com/Midoalone85/onebusiness-site/actions/runs/38711334118
