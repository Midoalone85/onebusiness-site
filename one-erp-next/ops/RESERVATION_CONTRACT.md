# ONE ERP Next | Stock reservation boundary (design and unit-tested policy)

**Status: draft only.** The original independent commerce prototype now has
`src/one_erp_core/reservations.py` and `tests/test_reservations.py`.
These are **pure calculation and authorization proposals**, NOT persisted
reservations, user-facing actions, or live Oracle features.

## Stock invariants

For each tenant, warehouse, and product, maintain:

- `0 <= reserved <= on_hand`
- `available = on_hand - reserved`
- For each confirmed sales-order line:
  `fulfilled + line_reserved <= ordered`
- `line_reserved <= warehouse/product reserved`; all line-held quantities
  sharing that stock bucket must sum to the persisted `reserved`.

Only active company stock administrators may propose holds or releases.
Accounting-only membership does not grant stock-processing privileges.
Cross-tenant, cross-warehouse and cross-product stock records are denied.
Quantities must be positive finite Decimal values with up to three decimals.
An event ID must be unique in the target tenant.

## Planned transactional write boundary (NOT IMPLEMENTED)

A future backend-only PostgreSQL service must:

1. Verify signed user identity and company membership from trusted sources
   BEFORE selecting the tenant-specific database; never trust client-supplied
   tenant or actor GUCs as authentication.
2. Require a confirmed sales-order line; lock order line and stock balance in
   a consistent order with `SELECT ... FOR UPDATE`, re-read the authoritative
   values, and check product/warehouse active status.
3. Persist a tenant-unique immutable hold/release event with a durable reference
   to the sales order line, then adjust per-line and global reserved quantities
   in the SAME database transaction.
4. Enforce atomic retries: the same event ID plus identical payload must be
   safely recognized according to the service's explicit idempotency contract;
   a reused ID with different input must be rejected.
5. Dispatch from this order's own reservation only, reducing BOTH on-hand
   and that line's reserved amount. Unreserved dispatch must never consume
   stock held for another order.
6. On cancellation/return, write compensating immutable events instead of
   deleting historical events. No silent overbooking or negative stock.
7. Use a dedicated limited runtime DB role, not the intentionally overpowered
   CI role; add concurrency tests for simultaneous competing holds, release vs
   dispatch and transaction rollback.

The existing `005_commercial_stock.sql` stores a global `reserved` balance
but has **no durable per-sales-line reservation or reservation ledger** yet.
Its existing dispatch path checks `on_hand - reserved`; it therefore cannot
consume held stock and must be extended before enabling live reservations.

## Release blocker

Do not expose reserve/release APIs, advertise the preview as live, merge this
draft as a production release, or modify the Oracle production database on
the strength of these policy tests. Financial posting, valuation, taxes,
cancel/reversal, stock concurrency, backup/restore and authenticated UI are
separate uncompleted integration gates.
