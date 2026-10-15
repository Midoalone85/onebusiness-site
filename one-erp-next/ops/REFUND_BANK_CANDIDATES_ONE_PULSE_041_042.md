# ONE ERP NEXT | 041–042 bank candidate controls and ONE PULSE

Checkpoint: 2026-10-15
Status: independent development feature branch. Tested only on disposable
PostgreSQL 14 with synthetic customer invoices and returns.

THIS IS NOT a bank payment, verified bank statement, settlement, payout
instruction, bank GL entry, ZATCA tax credit, signed document, or claim
of IFRS/SOCPA production compliance.

## 041: Persistent reference staging, not real bank ingestion

Source: db/041_refund_bank_unverified_registry.sql

The company accountant may submit a **manual bank-debit candidate** only if:

- An accepted returned item has a posted refund-payable claim (036).
- An immutable refund request exists and was independently approved (038).
- The candidate recorder is independent of the claim maker, order requester,
  and order approver.
- Tenant, order ID, exact SAR amount, bank date on/after internal approval,
  beneficiary evidence ref and payment reference all match.
- The transaction reference, bank-account reference and statement import
  reference have not already appeared together; one order has no second
  candidate. PostgreSQL constraints serialize racing inserts.
- The statement data and evidence reference are entered by a person and
  are NEVER regarded as verified bank-origin data.

The record is immutable, company scoped through dev RLS, and has exactly
one allowed verification state: "unverified". No "paid" or "bank-cleared"
status exists, and the stage never posts journal entries or touches
the actual customer liability balance.

## 042: ONE PULSE internal finance dashboard

Source: db/042_refund_control_pulse.sql

For each tenant, the finance read model compares the posted refund payable
with four mutually exclusive, non-final operational stages:

1. Recognized, not yet requested for payout.
2. Requested, waiting for independent checker approval.
3. Approved, no manual bank reference submitted.
4. Approved, manual debit reference provided but bank-origin unverified.

It checks source invoices and posted GL, order amounts, approval actor
separation, bank candidate amount/currency/beneficiary evidence, refuses
mixed-currency aggregation until IAS 21 FX policy exists, and reports
discrepancies and control reconciliation. A data entry cannot reduce the
total recognized refund liability. Cash status is always:
"bank_settlement_unknown_no_authenticated_feed".
This wording deliberately avoids asserting money has, or has not,
actually moved outside ONE ERP.

## Example

Previously paid SAR 10 against a SAR 25 original sale return:

- Revenue reversal = 25
- Trade receivables reduction = 15
- Customer refund payable = 10
- Requester enters internal 10 SAR payout request.
- Independent admin approves, no GL or bank payment created.
- Independent accountant submits a matching bank statement candidate.
- ONE PULSE now shows 10 SAR approved with *unverified reference*,
  not 10 SAR paid and not a 10 SAR reduction of the payable.

## Required before real settlement and release

- Physically isolated per-tenant database, server-authenticated actor,
  least-privilege functions, no caller-controlled auth GUC and formal
  maker/checker entitlements.
- Verified bank connection/statement provider, source integrity and
  ownership validation, bank statement transaction uniqueness across
  imports, beneficiary/account validation, reconciliation and approval.
- Independently signed payout instructions, transfer receipt IDs,
  external processing time/status checks, retry and failure handling.
- Controlled final Dr customer refund payable / Cr verified bank clearing
  (or appropriately documented bank cash) only once settlement is proven;
  afterwards reconcile clearing account to real imported bank statement.
- Proper Saudi ZATCA legal credit note, VAT tax period policy, reference
  to prior recognized refund claim with NO duplicate revenue reversal.
- Functional currency translation, real finance reporting period close,
  audit retention, operations monitoring, incident response and restore
  procedures, staged Oracle validation and local professional review.

CI evidence: commits e4ea2332 (041 SQL), ea5c9d4e (041 tests),
aaaae6d1 and 32aec269 (042 read model), 9e6c43c8 (042 tests).
All executable tests PASSED: 459/459 on Python 3.11 and 459/459 on
Python 3.13 with PostgreSQL 14 in:
https://github.com/Midoalone85/onebusiness-site/actions/runs/38741323292

No production database or external banking system has been touched.
