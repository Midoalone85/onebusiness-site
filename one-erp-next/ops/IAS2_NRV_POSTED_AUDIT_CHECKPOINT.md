# ONE ERP NEXT | IAS 2 posted valuation milestone (2026-10-15)

**Development only:** Draft branch \`feature/one-erp-smart-finance-20261015\`.
The IFRS / SOCPA inventory and credit routines here run only in disposable
PostgreSQL 14 CI. No tenant data, Oracle instance, ZATCA production endpoint,
purchase settlement, production tax invoice, or live user action is changed.

## Accounting outcome (concrete example)

Five units originally acquired for SAR 12.50 each:

| Detail | SAR |
|---|---:|
| Original recognized historical cost of inventory | 62.50 |
| Expected selling price, total | 50.00 |
| Estimated necessary selling costs, total | (5.00) |
| Estimated NRV | 45.00 |
| IAS 2 allowance for write-down | (17.50) |
| Net carrying amount | 45.00 |

On approved NRV write-down: **Dr Inventory NRV loss expense 17.50 /
Cr Inventory valuation allowance (contra asset) 17.50**. Original gross
inventory cost ledger stays at 62.50.

On later reviewed recovery of NRV to or above original cost:
**Dr Inventory valuation allowance 17.50 / Cr NRV expense reversal 17.50**.
Recovery is capped at historical cost. Original write-down/reversal event
history and a posted journal always remain linked and immutable.

## New development database migrations

- \`025_posted_inventory_nrv_allowance.sql\`: one company/warehouse/item
  allowance row, immutable source events, guarded debit/credit posting and
  period lock, source evidence tag, audit outbox, revision expectation,
  snapshot source validation, negative NRV and unsupported GL prevention,
  net cost report, atomic transaction and no duplicate event ID. The
  event is committed only with its posted journal.
- \`026_inventory_nrv_reporting.sql\`: company-wide gross cost, allowance,
  net inventory amount and source GL allowance reconciliations; period
  write-down/reversal aggregates and journal check. Reports enforce
  actor/tenant company membership and refuse mixed foreign currencies
  because a functional-currency conversion engine is not yet approved.
- \`inventory_nrv_policy.py\`: pure nonposting NRV and capped-reversal
  proposal with strict source/period checks, tested independently.

## Important deliberate limitation

If an item's active NRV allowance is positive, stock dispatch/receiving or
transfer that inserts a physical stock event **fails closed**. This prevents
a reported allowance remaining on missing stock. It is an engineering hold,
not a desirable production warehouse workflow. Before a usable inventory
release, the application must implement approved pro-rata allowance allocation
and derecognition on sales/returns/transfers/scrap, serial/lot and receipt
cost layers and related impairment-reversal rules.

A supplier billed return and its internal credit are already fully atomic in
migration 024 for strict homogeneous-cost, no-VAT single-receipt pilot; VAT
input recovery, FX, adjustments, multiple receipts, payment reconciliation
and ZATCA legal credit workflows are not implemented.

The following are not yet guaranteed:
- IFRS-compliant item grouping/NRV for materials and firm contracts.
- Human evidence verification and independent approval segregation.
- Receipt layer or lot costing across FIFO/specific-id/weighted average.
- NRV carrying balance integrated into sold inventory / period close.
- Global chart-of-accounts vs actual GL aggregate reconciliation and FX.
- Functional currency, VAT, country packs, tax note disclosures, signing,
  external audit evidence or independent SOCPA review.
- Proper production least-privilege DB roles and authenticated tenant context.

This is an auditable **development component**, not certification of
IFRS/SOCPA compliance or a production-ready ERP. Changes are restricted to
the draft GitHub feature branch. CI evidence is tracked in the PR thread.
