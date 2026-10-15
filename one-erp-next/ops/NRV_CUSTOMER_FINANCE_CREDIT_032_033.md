# ONE ERP NEXT | 032-033 reviewed NRV customer return credits

**Date:** 2026-10-15. **Development branch only:** feature/one-erp-smart-finance-20261015.

This is a **non-taxable, internal-accounting pilot**, not a Saudi ZATCA-compliant legal credit note, refund instruction, invoice send, production authorization boundary, or verified IFRS/SOCPA attestation.

## Source to finance sequence

1. Customer sale physically dispatched, financially costed and source-matched to a **previously issued** customer invoice.
2. Customer return inspected and accepted into **quarantine** by migration 030. Original gross cost restored, independent new IAS 2 NRV assessed and supported by a posted GL journal.
3. Migration 031 verifies the RMA, source dispatch, gross stock restoration, quarantine stock, revised allowance and posted GL.
4. **032** `one_issue_reviewed_nrv_customer_credit` locks the completed RMA and original invoice, verifies 031, requires a single issued out-of-scope tax-free line from the source invoice, the same tenant/partner/currency/account, original billed quantity and uncredited accepted RMA quantity. Then, in one PostgreSQL transaction:
   - **Dr original sales revenue / Cr trade receivables** for the goods return amount at original invoiced selling price;
   - post the credit's GL journal to an open accounting period;
   - create and internally issue the source-linked customer credit document (not legal tax issuance);
   - immutably link source original customer invoice, completed NRV RMA, new credit, original reviewed NRV event and finance audit/outbox.
5. **033** `one_nrv_customer_credit_case` is read only. It distinguishes `awaiting_internal_credit` from `internal_credit_posted`, `qa_discrepancy` and `financial_discrepancy`, including QA quantity, gross source cost, NRV allowance, net quarantine carrying amount, credit amount/quantity and source-linked posted journal.

### Two books, one underlying return, never confused

Illustrative source: returned 2 items sold at SAR 12.50 each; their original gross cost is SAR 25 and their reassessed QA NRV is SAR 16 with SAR 9 allowance.

**Stock journal** had already restored Dr QA inventory 25 / Cr COGS 25 and Dr NRV loss 9 / Cr stock allowance 9 in the reviewed RMA transaction.

**Customer finance journal** now independently posts Dr sales revenue 25 / Cr customer receivable 25, with a separate immutable link to the *already accepted* physical RMA and the original customer invoice. Never post the gross stock value as a customer financial refund. No bank transfer or cash payment is triggered by these entries.

### Fail closed

- Reject wrong company/actor or unknown/non-accepted RMA, false QA/GL reconciliation, repeated accepted return credit, wrong customer order line, wrong source revenue/receivables account, more credited than invoiced quantities/amounts, wrong period, non-finite amounts and duplicate document reference.
- Out-of-scope `NONE` no-VAT internal source only, single sales line and no discount. VAT-rated source must wait for a reviewed statutory tax pack, proper official note and invoice/return linkage.
- A source invoice with settled payments cannot be overcredited; a refund/overpayment handling workflow requires separately approved allocation and payment/bank reconciliation.
- QA remains quarantined; this finance-only workflow never moves goods into sellable stock or changes the NRV assessment.
- All 032 financial rows roll back as one SQL transaction on failure. The *earlier* completed QA return remains immutable and available for a corrected finance attempt. Hence do **not** claim 030 physical QA and 032 finance issue share one end-to-end transaction.
- CI SQL GUC contexts and broad development grants are not secure production authentication. Authorization must be from verified server-side identity with tenant-selected isolated database and least-privilege roles.

## Later release gates

Country tax/VAT credit pack + ZATCA official debit/credit logic, nonreturn allowances and discount policies, FX and currency translations, payment allocations/refunds, partial invoice-line and multi-delivery traceability, supplier/customer statements, statement reconciliation, protected statutory invoice sequencing/signatures, approval segregation and production security/DR. Do not merge, deploy or represent as ready for customers based on local test success.
