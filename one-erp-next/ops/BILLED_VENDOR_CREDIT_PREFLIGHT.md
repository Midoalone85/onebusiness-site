# ONE ERP Next | 024 preflight: billed supplier return / AP credit

**Scope (2026-10-15):** isolated finance-development branch only. The
existing 022/023 financially valued **unbilled** supplier return remains intact.
The new vendor-credit eligibility model is a pure Python **preflight** and
must not be represented as a database operation, live invoice, credit memo,
external refund, tax reversal, Oracle deploy, or released feature.

## The critical accounting distinction

- **Unbilled receipt return (migration 022):** debit GRNI, credit
  Inventory, and remove physical/book quantity in one DB transaction.
- **Billed receipt return (NOT operational yet):** debit Accounts Payable,
  credit Inventory, and remove physical/book quantity, with a source-linked
  vendor credit and immutable posted journal **in one DB transaction**.
- A billed vendor return must **never** debit GRNI, and a no-tax internal
  vendor credit must **never** create a second expense for goods already
  recognized into inventory.
- VAT recovery, nonrecoverable input VAT, price/cost differences, foreign
  exchange, landed costs, warranties, cash refund and ZATCA-compliant external
  supplier credit require separately audited policies. Reject, don't guess.

## What was added

Source module: src/one_erp_core/vendor_credit_policy.py

Function: propose_billed_vendor_credit(...)

It checks a complete trusted source snapshot against exactly one proposed
vendor-credit invoice line, including:

- tenant, supplier, currency and linked original issued vendor bill;
- original valued receipt, pending supplier return and matched PO bill line;
- all previously returned, billed and credited quantities;
- available unreserved physical stock and stored valuation;
- same original unit price, zero tax and no discount;
- original receipt cost = current book-average cost = credit price;
- currency precision and exact zero-balance behavior.

The result is an immutable proposal (Dr AP, Cr Inventory, quantity and
book value after hypothetical movement). There is NO physical shipment,
AP settlement, external invoice issue, journal posting, HTTP handler or actor
authorization in this module. HTTP-provided source evidence is untrusted.

Test module: tests/test_vendor_credit_policy.py, 18 regression cases.

## Next release-blocking engineering slice

1. Add source-linked PostgreSQL billed return/credit allocations with
   immutable tenant-scoped FKs and RLS. Keep the unbilled and billed
   accounting paths distinct at the DB boundary.
2. Implement a single atomic operation with trusted authenticated actor,
   tenant database selected server-side, order/return/source-bill and stock
   locks, duplicate IDs, financial source matching and posted-journal
   validation before RMA completion. Credit document, AP/Inventory journal,
   stock value event and physical movement must commit or all roll back.
3. Prove rollback for wrong accounting period/accounts, partial bills,
   concurrent claims, duplicate retries, unvalued or reserved stock, and
   cross-tenant access in PostgreSQL 14.
4. Add net AP/vendor credit reports and RMA source timeline events. Keep
   GRNI reconciliation for unbilled returns unchanged.
5. Add KSA input VAT, official supplier credits, payments/refunds, cost
   variance and landed cost only under separate approved tax/cost rules.

**Do not expose preflight as a working return button.** CI broad grants and
SQL session GUCs are not production authentication. Do not merge or deploy.
