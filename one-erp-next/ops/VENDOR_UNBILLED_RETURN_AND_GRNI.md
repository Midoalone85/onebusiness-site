# ONE ERP Next | Supplier return + valued GRNI reversal | CI prototype
Date: 2026-10-15. Branch only, not merged, not Oracle.

## Verified engineering intent and scope

ONE Returns Hub treats supplier returns as physical movements AND accounting,
never a standalone vendor credit memo or an unvalued stock adjustment.

### New migration 022: unbilled supplier return

- A customer-isolated and role-guarded RMA, opened against one immutable
  original **purchase_receipt** stock event.
- The receipt must already be valued and posted into Inventory Dr / GRNI Cr.
- It must still be **unbilled** at the PO-line level for the entire return
  quantity. Previously billed goods REQUIRE a vendor credit/AP route that is
  not yet implemented. A return cannot consume customer-reserved stock.
- A conservative cost policy requires original receipt unit cost to agree
  with current book weighted-average unit cost; mixed-cost warehouses cannot
  silently guess a variance. They must await a cost-layer/variance solution.
- Full accepted unbilled supplier return posts Dr GRNI / Cr Inventory,
  decrements the physical warehouse stock and stored inventory book value,
  creates an immutable return-cost link and valued stock event and stock
  movement audit. All happen in the SAME PostgreSQL transaction.
- Failure on period, account, role, quantity, currency, mixed source cost or
  physical allocation leaves the return requested, with no physical movement
  or accounting journal.
- The existing PO/vendor-bill matcher has been tightened so future bills
  subtract *all* physical supplier-return quantities, including unvalued
  legacy returns. This prevents accidental duplicate billing.

### New migration 023: supplier return reporting

- Existing tenant/role-scoped ONE PULSE cost readout now computes:
  **GRNI outstanding = total valued receipts - matched supplier-billed
  amount - financially valued supplier return amount**.
- The current report still displays gross receipts separately and flags
  unvalued legacy vendor return events, rather than masking them.
- Supplier RMA timeline now includes the linked immutable posted GRNI
  reversal event alongside original receipt, return request/completion,
  and stock movement.
- These are PostgreSQL read functions, NOT authenticated live browser APIs.

### Small illustrative case (SAR, no tax)

PO receives 5 units at SAR 12.50, Inventory Dr 62.50 / GRNI Cr 62.50.
Vendor physically receives 2 returned unbilled units: GRNI Dr 25.00,
Inventory Cr 25.00. Stock remaining 3 units worth SAR 37.50. If the
supplier later bills the remaining 3 units, vendor bill posts
GRNI Dr 37.50 / Accounts Payable Cr 37.50. GRNI becomes zero.
An invoice for 4 units is rejected because only 3 retained units remain.

## Important unimplemented scenarios

- Goods ALREADY invoiced by supplier require **vendor credit**, AP credit,
  VAT/input-tax reversal, payment netting/refund, full invoice/source/tax
  matching, and possibly foreign exchange variance. They are blocked,
  not silently processed as an unbilled stock return.
- Mixed purchase costs / landed cost and supplier price variances need
  dedicated cost layers and a reviewed variance account, not this strict
  homogeneous-cost pilot.
- Actual pickup/delivery to vendor, carrier proof, partial vendor approval,
  warranty replacement, RMA authorization workflow, serial/lot/expiry/bin
  scanning and photographic evidence are NOT implemented.
- Stock return, valuation, and counterparty finance need authenticated API
  commands and least-privilege access to physically isolated tenant DBs.
  PostgreSQL session GUCs and the deliberately overprivileged disposable
  CI DB runtime role are **not authentication**.
- Existing legacy direct unvalued movements remain allowed in CI for
  backwards test coverage. Production MUST retire or revoke them.
- No VAT/ZATCA compliant invoice/credit, real payment, Oracle deployment,
  live inventory update or actual customer/vendor transaction is performed.

## Verified CI acceptance

- **335 / 335 tests under Python 3.11 and 335 / 335 under Python 3.13**
  against disposable PostgreSQL 14.
- GitHub CI: https://github.com/Midoalone85/onebusiness-site/actions/runs/38717071409
- A supplier return of 2 from 5 units makes a bill for 4 invalid, while the
  retained 3 can be billed. Reported outstanding GRNI SAR 37.50 before
  that invoice, then zero after.
- An unvalued legacy physical vendor return is correctly flagged for finance
  reconciliation rather than silently accepted.
- Concurrent supplier billing and vendor return claiming 3 of 5 units in
  separate transactions cannot both succeed.
- This does NOT include invoiced goods vendor credit, AP/VAT reversal,
  settlement, mixed-cost variance, lot/serial custody, real identity/API,
  ZATCA, Oracle deployment, or production recovery.
