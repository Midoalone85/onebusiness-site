# ONE ERP NEXT | 036-037 Customer Refund Liability & Audit Register

**Date:** 2026-10-15. Scope: isolated feature branch / disposable PostgreSQL 14.

**NOT** cash disbursement, bank connection, customer-facing refund,
electronic statutory credit, VAT return amendment, ZATCA signing/clearance,
production-grade user authentication or IFRS/SOCPA certification.

## 036 | Internal monetary liability recognition

The previously accepted and NRV-valued RMA is a distinct, immutable
warehouse transaction (030-031). The customer invoice, customer receipts,
customer credits and all earlier posted refund claims must be matched under
an original-invoice row lock. No refund claim may rely on HTTP-provided
settlement figures or a pasted unaudited source snapshot.

- If the original **SAR 25** sale return has **SAR 10** cash applied and
  the remaining **SAR 15** in Accounts Receivable, post:
  **Dr original sales revenue 25 / Cr trade AR 15 /
  Cr customer refund payable 10**.
- If the same source is **fully paid SAR 25**:
  **Dr original sales revenue 25 / Cr customer refund payable 25**.
  Never credit trade AR into a disguised negative receivable.
- If the source is **entirely unpaid** (no refund obligation):
  route to existing 032 internally issued AR credit instead.
- Sales return value, accepted RMA quantity, reviewed QA source and original
  invoice must match. Posted claims cannot exceed source invoice gross, nor
  can refund liabilities exceed the actual previously applied cash.
- The internal claim is unique per accepted RMA and linked to a single posted,
  immutable GL journal. GL/accounting-period failures roll back claim and all
  new entries together. Future ordinary credit linkages on the *same RMA* are
  blocked; source invoice credit notes that would exceed posted recognized
  claims are likewise rejected.
- A later applied customer receipt on the original invoice is blocked when
  paid + issued credits + already recognized claim AR reductions would
  exceed the original invoice gross. This prevents overcollection of
  receivables already derecognized by the claim.
- Taxed source invoices, variable pricing, discounts, payment gateway
  returns, loyalty/stored credit, complex contracts, FX, partially attributed
  receipt lines, original invoices with unresolved QA and multi-currency
  allocations are rejected or not supported until separately approved.

**Important conceptual boundary:** the claim recognizes a financial
obligation, not a finalized statutory tax credit document. In jurisdictions
requiring statutory credit documentation, a subsequent tax issuance must
attach to this *existing* recognition without reversing revenue twice.
The final legal invoice lifecycle and original customer VAT-period
accounting have NOT been implemented here.

## 037 | Refund liability audit read model

The company report groups posted refund claims by accounting period
and compares each recognized return with:
- QA inspected source, accepted physical units and cost assessment;
- original issued sales invoice and currency;
- posted source journal, total debits, total credits;
- actual journal debit to revenue;
- actual credit to the source invoice's AR account and distinct refund
  liability control account;
- immutable claim quantities, AR and refundable amounts.

It reports number of claims, source value, AR reversed, refund liabilities
recognized, discrepancies and overall reconciliation status. Missing QA
evidence is flagged rather than hidden by an inner join. It refuses mixed
currencies without functional-currency conversion and enforces company
membership/RLS.

**The register is not a statement of cash paid or bank-cleared amounts.**
No refund outgoing payments, fee offsets or refund-liability settlements
exist in this development slice.

## Next gated steps (NOT DONE)

1. Separate maker/checker approval workflow for refund payment orders,
   partner/bank beneficiary verification, controls for changed bank details,
   approved payout reason and limits.
2. A legal credit note referenced to original tax invoice, period-bound
   Saudi tax policy, official ZATCA UBL signing, hash, QR/ICV, clearance or
   reporting acknowledgments where applicable. Existing IFRS accounting
   obligation must link to legal document without duplicate revenue posting.
3. Refund disbursement GL (Dr approved customer refund liability, Cr bank
   clearing), with bank statement reconciliation and failed/refused payout
   recovery, plus netting controlled by customer agreement.
4. Company bank account isolation, tenant actor identity, least-privilege
   DB roles, KMS/HSM and idempotent payout queue, digital audit/retention,
   operational monitoring, alerts, tested backups and staging.
5. Full IFRS as locally endorsed or eligible IFRS for SMEs, relevant
   revenue/returns/tax disclosure choices, a qualified Saudi practitioner
   review, and period close validation.

Do not migrate or deploy these experimental tables to Oracle production or
advertise them as a Saudi-compliant refund workflow. Passing CI means code
tests passed in a disposable dataset, nothing more.

## Concurrency and CI evidence

The original RMA lock is acquired before the original invoice row lock,
consistent with the existing return-backed credit operation. Payment
allocation serializes on the same original invoice. Concurrent refund
recognition attempts for a single inspected RMA are verified to produce
exactly one posted claim and one original-liability recognition, with the
second transaction rejected. A subsequent cash receipt allocated against
AR already derecognized by the claim is blocked and completely rolled back.

PostgreSQL 14 CI for this feature completed with **441 tests passing under
Python 3.11 and 441 passing under Python 3.13**:
https://github.com/REPOSITORY_OWNER/onebusiness-site/actions/runs/38735719927

These numbers are development-test evidence, not sign-off for production,
external payment rails, official tax notes, or statutory compliance.
