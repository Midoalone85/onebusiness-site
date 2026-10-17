# ONE ERP NEXT | Refund control checkpoint: stages 038–040

Date: 2026-10-15. Branch: feature/one-erp-smart-finance-20261015.
**Development proof only, NOT deployed, NOT banking, NOT statutory ZATCA issuance.**

## 038 | Maker/checker approval

An approved internal refund order is based on an *already posted* 036 customer
refund-payable claim, tied to a QA-accepted source RMA, customer invoice and
tenant. A company accountant/admin requests with a reference to beneficiary
evidence and documented reason. A distinct company administrator who is
neither the order requester NOR original claim maker must approve with a
separate approval evidence reference. Original partner, currency and exact
liability amount are from the trusted DB, never a browser amount.

Order identity, economic terms and evidence cannot change after request.
Approvals cannot be replayed. An immutable request/approval audit is created.
Only ONE refund order is permitted per accepted RMA. A mere evidence reference
is not proof that the banking beneficiary has been verified independently.

## 039 | Approval finance overview

Authorized tenant reader sees pending amount, approved-but-unpaid amount,
claims without orders, source-GL mismatches, audit mismatches and whether
source reconciliation holds. Detail preview of an approved instruction is
explicitly labeled **approved_not_disbursed**. No journal or bank cash is
touched by request, approval, reporting or preview. Currency mixes refuse
aggregation without reviewed functional-currency conversion.

## 040 | External bank debit match candidate

A PURE, non-posting policy compares a bank statement *candidate* to a
previously approved voucher: amount, currency, tenant, beneficiary evidence,
date, order ID in bank reference and statement-import reference. It enforces
separate maker/approver. Result can only say
**candidate_match_manual_review_required**, NOT "customer paid".

There is NO authenticated bank feed, signer/key confirmation, paid record,
unique bank statement ledger, bank API, payout provider, actual customer
refund, bank reconciled GL entry, or external payment status update.
User-entered banking evidence cannot establish an authentic statement.

## Example (SAR)

For an NRV-reviewed accepted return of 25, when 10 has been paid:
- DR original revenue or sales return 25
- CR accounts receivable 15
- CR customer refund liability 10
- Internal order requests/approves payment of **10**.
- Financial state remains **approved, NOT disbursed**.
- A hypothetical bank debit for 10 may be proposed as a candidate only.

## Required before production release

1. Enforce backend-verified OIDC identity, physically isolated tenant DB,
   narrowly granted SQL roles. DEV broad-grant role and session GUCs are
   NOT production authorization controls.
2. Complete legal/tax credit note (KSA ZATCA) and attach it to existing 036
   liability without repeating revenue or VAT. Government schema/signature,
   numbering, clearance/reporting, tax period, EGS identity all remain open.
3. Verified customer beneficiary and bank information, high-risk changes,
   fraud limits, multi-signature approvals and provider integration.
4. Bank-origin verified statement and payout reconciliation with replay-proof
   settlement IDs; only then post DR customer refund payable / CR cash or
   bank clearing. Handle rejected, missing, returned and partial transfers.
5. Complete bank and customer account statements, FX, close, aging, IFRS
   presentation and SOCPA professional review. Stage and test on Oracle
   sandbox before approved production deployment.

All real money movements remain DISABLED. This checkpoint is a bounded
development milestone, not completion of the entire ONE ERP NEXT SaaS.
