# ONE ERP Next | Accepted customer return historical-cost and QA disposition
15 Oct 2026 | migrations 019, 020 and 021 | PostgreSQL 14 disposable CI only.

This is an independent software platform, not an Odoo addon.
All changes are in a DRAFT GitHub branch and have NOT been merged or deployed
to Oracle or used with customer, patient or bank records.

## Source to final outcome, with ONE original case identifier

1. The sale dispatch must already have a posted, immutable valuation event.
   Its warehouse quantity was reduced and GL posted **Dr COGS / Cr Inventory**.
2. Customer return request points to that original sale movement. Only an
   active company-admin may accept returned units physically.
3. NEW `one_accept_and_value_customer_return` accepts physical returned stock
   into a non-sellable quarantine warehouse, derives cost **strictly from the
   original sale dispatch's recorded cost** (not from the resale price or today's
   average price), updates the quarantine value ledger and posts a balanced
   **Dr Inventory / Cr COGS** reversal in the SAME transaction.
   An error in source, GL account, period, currency, quantity or valuation
   reverses the physical stock change, the return status and all journals.
4. The immutable `one_customer_return_cost_links` table records source
   dispatch, return case and accepted unit/cost allocation. Partial returns
   cannot collectively reverse more quantity/value than the original
   valued dispatch. Rounding residue goes to the terminal fully returned
   units rather than creating extra cost.
5. NEW `one_resolve_and_value_customer_quarantine` requires a costed
   accepted return, preserving the same RMA identity. For QA **restock** it
   moves source warehouse book quantity AND original cost into an active
   saleable warehouse together with physical transfer events. There is no
   extra GL income or expense when the same inventory asset account is used.
6. For QA **disposal**, it deducts original historical cost and quantity from
   quarantine and posts **Dr Disposal expense / Cr Inventory** in one
   transaction. A failed period or journal rolls back the disposal itself.
   Disposal is neither a cash refund nor customer credit.
7. NEW `one_return_disposition_costs` makes per-QA action cost allocation
   immutable, preventing double restock/disposal and over-allocation.
8. Migration `021_accounted_returns_reporting.sql` extends ONE PULSE and
   RMA timeline to recognize fully costed stock restock/disposal movement
   pairs, and joins COGS-reversal/disposal GL posting events to the same RMA.
   Old/manual unvalued movements remain visible as reconciliation failures.

## Numeric illustration, not live company data

A stock item with original unit cost SAR 12.50 is sold, then 2 units return.

- Accepted 2 units: quarantine +2 units / +SAR 25; Dr Inventory 25,
  Cr COGS 25.
- QA restocks 1 unit: quarantine -1 / -SAR 12.50, sellable warehouse +1 /
  +SAR 12.50. Inventory-to-inventory transfer, no income is invented.
- QA disposes 1 unit: quarantine -1 / -SAR 12.50, Dr Disposal expense
  SAR 12.50, Cr Inventory SAR 12.50. Quarantine ends with zero units/value.
- A separately approved customer credit note reverses revenue/tax treatment,
  not stock again. That internal credit remains controlled by its source
  invoice and accepted-return quantity constraints.

## Tested and independently verifiable

- Original valued source prevents sale-price-based return valuation.
- Successful acceptance writes physical return, costed stock value,
  original COGS reversal, immutable case link and GL audit.
- Invalid period/GL role/cross tenant attempts do not leave a return
  movement, valuation record or posted journal.
- Duplicate acceptance or disposition and over-accepted/disposed source
  quantities are rejected.
- QA partial restock+disposal transfers correct book cost without losing or
  inventing stock units; disposal GL journal has verified expense/asset sides.
- ONE PULSE marks fully valued QA stock bucket reconciled and correctly flags
  unvalued legacy return. The RMA timeline includes accepted return and
  disposal cost postings.
- **327/327 tests pass on EACH Python 3.11 and 3.13**, disposable PostgreSQL 14:
  https://github.com/REPOSITORY_OWNER/onebusiness-site/actions/runs/38715894903

## IMPORTANT: remaining production blockers

- The older direct `one_finish_return`,
  `one_resolve_return_quarantine`, direct stock receipt/dispatch and
  separate-cost functions remain executable in disposable CI. Real
  production must use a trusted operator/API boundary that revokes
  access to unvalued entrypoints to avoid mismatches.
- There is no per-case physical bin/serial/lot ownership of quarantine.
  Cost links track monetary provenance but do NOT establish a scanned
  custody chain for a specific returned physical device or package.
- No actual country-approved VAT/ZATCA credit-note issuance, customer cash
  refund, replacement logistics, warranty repair, freight, tax on scrapped
  inventory, FX, landed-cost amendments, source GL account matching by
  business rules, branch consolidation or full inventory-to-GL period close.
- Historical cost is based on original valued dispatch and existing
  inventory asset/COGS account parameters. Real production must bind these
  accounts to versioned product/warehouse tax and accounting policies,
  prevent operator overrides and handle valuation policy changes.
- A physical return's acceptance can reverse COGS before final customer
  settlement. This is an explicit pilot accounting policy, not a claim of
  universal accounting-standard compliance; country-specific approval
  and contract/rights-to-recover review are mandatory.
- The value positions aggregate warehouse+product (single currency);
  they are not lot-specific or globally FIFO. Precision, mixed currency,
  disposal tracking and partial acceptance need an end-to-end pilot.
- The linked SQL RMA timeline is NOT a network worker, not an authenticated
  REST API and not live analytics. SQL session GUCs are NOT authentication.
- Production tenant DB isolation, authorized operator verification, immutable
  audit, signed actions, least-privilege DB roles, backup restore, security
  testing and live UI connectivity remain mandatory.
