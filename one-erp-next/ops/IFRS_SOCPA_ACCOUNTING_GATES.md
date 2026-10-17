# ONE ERP Next | Accounting policies and Saudi reporting gates

Date: 2026-10-15. Engineering controls, NOT an assertion of accounting,
statutory, audit, tax or SOCPA compliance for a live company. Backend work
remains in a draft development branch with synthetic PostgreSQL test data.

## Reporting framework must be selected per legal entity AND period

- Publicly accountable KSA entities: full IFRS Accounting Standards
  **as endorsed in Saudi Arabia** plus other SOCPA-endorsed standards,
  pronouncements and disclosures.
- Eligible SMEs: endorsed IFRS for SMEs, unless validly electing to apply
  the complete set of endorsed full IFRS requirements; never mix individual
  full-IFRS and SME options opportunistically.
- The 2025 third-edition IFRS for SMEs replaces the earlier edition for
  annual reporting periods starting on/after 2027-01-01; early adoption
  is possible where locally endorsed and appropriate.
- Full IFRS 18 supersedes IAS 1 for annual reporting periods beginning
  on/after 2027-01-01; earlier adoption is possible, with transition/
  comparative requirements. Implement rule versioning, not hardcoded
  2026 financial statement headings for 2027 books.
- SOCPA-endorsed country requirements, including Zakat guidance, are
  **not interchangeable** with ZATCA VAT/e-invoicing requirements.

Verified standards/resources:
- IFRS Saudi jurisdiction: https://www.ifrs.org/use-around-the-world/use-of-ifrs-standards-by-jurisdiction/view-jurisdiction/saudi-arabia/
- SOCPA endorsement material: https://socpa.org.sa/Socpa/Pages/Knowledge-Center/101.aspx?lang=en-us
- IAS 2: https://www.ifrs.org/issued-standards/list-of-standards/ias-2-inventories/
- IFRS 18: https://www.ifrs.org/issued-standards/list-of-standards/ifrs-18-presentation-and-disclosure-in-financial-statements/

## Ledger rules by standard (engineering acceptance criteria)

| Standard | Operational gate |
| --- | --- |
| IAS 2 / SME inventory requirements | Approved inventory category, specific identification where appropriate or FIFO/weighted average for interchangeable goods; include freight/import and directly attributable nonrecoverable costs, exclude recoverable VAT and unearned discounts; lower of cost/NRV; impairment/restoration controlled by actual evidence, period and approvals |
| IAS 21 | Record functional vs transaction currency explicitly, source rate and rate date; recognize AP foreign-exchange movements separately; disallow unsupported FX in tax-free single-currency RMA |
| IFRS 15 | Revenue only when performance obligation satisfied (typically control transfer), not solely invoice creation; returns, variable consideration and refund liabilities need contract-aware treatment |
| IFRS 9 | Customer ageing, expected credit losses with documented forward-looking provisioning and write-off approvals; never hide ECL inside stock valuation |
| IAS 16 / IAS 38 | Capitalize and depreciate/amortize eligible assets/intangibles, track disposal, impairment and accounting policy |
| IFRS 16 | Recognize relevant lease liabilities/right-of-use assets and effective interest, subject to valid exemptions |
| IAS 19 | Employee benefit accruals/actuarial calculations as relevant |
| IAS 12 and SOCPA-specific Zakat | Present income taxes and Zakat based on entity status and approved accounting/tax basis; do not substitute VAT payable |
| IAS 1 (2026) / IFRS 18 (2027 full IFRS) | Controlled financial statement classification, comparative presentation, audit evidence and applicable disclosures |

## 024 | Billed supplier return, current isolated PostgreSQL v1

Already simulated with atomic database operations:
1. Confirm original receipt, single PO line / one receipt source, stored
   valuation, issued vendor bill and exact matched line/partner/currency.
2. Lock PO and RMA; verify remaining original bill quantities, all previous
   vendor credit totals, unreserved physical stock and matching book value.
3. Reject VAT, discounts, differences in purchase price vs booked stock cost,
   cost-mixed buckets and ambiguous second receipts (no silent expense).
4. Same PostgreSQL transaction: complete source RMA physical movement,
   deduct valued inventory, post Dr AP / Cr Inventory, seal an internal source-
   linked vendor-credit document and append immutable stock and audit events.
5. GRNI readout excludes these already-billed AP returns; only the distinct
   unbilled return links in 022 net GRNI. RMA case timeline includes credit
   and journal event references.
6. Full transaction rollback if period, GL account, source, amount or
   quantity validation fails; unit and integration test suites assert this.

Boundary conditions: currently no registered live user HTTP command, no
verified legal tax credit note, no cash refund, no approved multi-receipt
cost allocation, no vendor approval signatures, no lot/serial/expiry,
no tax jurisdiction pack and NO Oracle deployment. An RMA with a previously
paid bill may create a supplier receivable/debit AP that requires separate
controlled netting/refund and presentation.

## Release gates, in dependency order

1. Verified IdP -> signed operator/tenant identity -> physically separate
   tenant DB and least-privilege backend role. User-controlled SQL GUCs and
   disposable CI broad grants are never permitted in production.
2. Approved configuration per tenant: entity classification, financial
   framework, effective dates, functional currency, chart mapping,
   costing method and inventory disclosures.
3. Replace pilot PO-as-receipt source assumptions with receipt-level billed
   allocations and cost layers, lot/serial/bin, quality and carrier evidence,
   actual carrier/vendor authorization, multi-line/partial exchange.
4. Add a verified KSA tax profile (ZATCA rules/official supplier credit
   references, recoverability, FX and transport), country-specific invoice
   and post-period correction rules, protected sequential legal numbering,
   controlled issuance and audited approvals.
5. Reconcile physical stock to stock valuation, GL inventory, GRNI, AP
   subledger, payment clearing and period close for every legal entity.
6. Test real secured API, concurrency/replay/crashes, proofed restore,
   signing and key rotation, capacity/monitoring, audit, staging pilot,
   independent professional review before release.

## Critical invariant

Correct double-entry and locally passing CI tests are **necessary but not
sufficient** to claim full IFRS/SOCPA compliance. External reporting requires
appropriate accounting-policy elections, completeness, judgement, verified
numbers, reconciliations, disclosures, tax law and professional sign-off.


## IAS 2 NRV preliminary control, added 2026-10-15

Pure-policy module: src/one_erp_core/inventory_nrv_policy.py
Tests: tests/test_inventory_nrv_policy.py

Given trusted tenant/item/warehouse original inventory cost, current
carrying amount, independent source-evidenced expected selling price and
the necessary completion and selling costs, the engine compares original
cost to estimated NRV. It proposes an expense/write-down entry or an
expense reversal capped by the original cost (never revalue inventory
above the unreduced original cost). Exact monetary precision, reporting
period, company, currency and approved GL accounts are validated.
A proposal does NOT post, alter balances, assert external market values,
or prove approval. Inventory item grouping, FIFO/specific-id source cost,
zero-NRV carrying buckets, write-down ledgers, accounting-policy change
controls, disclosure and actual atomic ledger posting remain prerequisites.
