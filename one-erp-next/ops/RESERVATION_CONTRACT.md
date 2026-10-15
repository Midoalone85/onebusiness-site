# ONE ERP Next | Commercial inventory reservation boundary

**State: independent prototype, NOT deployed.** Python pure-domain proposals,
PostgreSQL 14 reservation migration and repeatable CI tests exist on the draft
finance branch. No Oracle application/server/customer data was changed.

## Domain and storage

- `src/one_erp_core/reservations.py`: side-effect-free hold/release plans
  requiring active company stock-admin membership, a confirmed sale, tenant /
  location match, finite Decimal quantities with three-place precision, and a
  new event ID.
- `db/006_stock_reservations.sql`: per-sales-line `reserved_qty`, immutable
  `one_reservation_events`, RLS, plus security-invoker SQL functions
  `one_reserve_sale_line`, `one_release_sale_line` and
  `one_dispatch_reserved_sale_line`.
- `tests/test_reservations.py`, `tests/test_postgres_integration.py`:
  pure-domain and disposable PostgreSQL integration tests, including competing
  reservation transactions.

## Invariants

- `0 <= warehouse reserved <= on_hand`
- `available = on_hand - reserved`
- `0 <= line fulfilled + line reserved <= ordered`
- A reservation for sale order A must not be released or consumed on order B.
- Reservation events cannot be edited/deleted; a repeated event UUID is rejected.
- Hold, release and reserved dispatch each lock their commercial order and
  line, update the stock bucket conditionally, and persist the event in one SQL
  transaction. Failed writes roll back the whole transaction.

The migration does **not** provide a free-form reservation cancellation,
returns, stock valuation, FIFO/weighted-average costing, tax calculation,
invoice issuance, actual ledger posting or authenticated HTTP endpoint.

## Critical trust boundary

The new functions are `SECURITY INVOKER` and are revoked from `PUBLIC`.
Tests deliberately grant broad access to `one_erp_runtime_ci` in an
isolated disposable database. These grants MUST NOT be reused in production.

Existing `one.tenant_id`, `one.actor_id`, and `one.commerce_context`
session variables can be set by direct SQL callers, and therefore are
**not proof of authentication, tenant rights, or administrative approval**.
No production-facing role should be granted unrestricted table DML or
EXECUTE on these routines until a genuinely authenticated, narrowly
privileged backend has been implemented and security reviewed.

Before live use:

1. Verify trusted identity plus tenant entitlements **before** selecting the
   isolated company DB; refuse impersonation, disabled users and tenants.
2. Replace CI grants with narrowly scoped backend identities and independently
   enforced server-side permissions.
3. Add durable idempotency for retries: same event and identical payload must
   return the approved outcome; conflicting reuse must fail. Current SQL
   rejects all duplicate event IDs, including exact retries.
4. Add cancellation, return/compensation and allocation history APIs. Do not
   delete audit events or allow unaudited changes to reserve counters.
5. Test under stronger concurrency, deadlock/retry scenarios, realistic loads,
   stock valuation and invoice/journal reconciliation.
6. Verify full encrypted backup + restore, HTTPS, certificate, role/network
   boundaries and tenant provisioning; independent security review remains
   mandatory.

The standalone finance + stock system is **not yet a published commercial ERP**.
The static browser demo is fictional and disconnected from this database.
