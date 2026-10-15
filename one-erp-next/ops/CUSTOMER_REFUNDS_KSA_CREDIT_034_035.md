# ONE ERP NEXT | 034-035 customer refund exposure and KSA credit source gates

Checkpoint: 2026-10-15, isolated feature branch. **This is NOT a ZATCA-approved electronic credit note, a monetary refund, an invoice integration, an approved tax return adjustment, or an IFRS/SOCPA compliance declaration.**

## 034: Distinguish AR reduction from amounts that may need refund

The read-only function \`one_nrv_return_settlement_exposure(tenant,actor,RMA,source_invoice)\` combines:

- Verified company actor and previously accepted/valued NRV return (030-033).
- Original issued customer invoice and its actual sales-order / invoice-line allocation.
- Total applied cash receipts allocated against the invoice.
- All already-issued customer credits for that original invoice.
- Accepted goods-return value at original selling price, an illustrative output tax amount and a *potential* AR adjustment vs amount that could require a separate refund liability.

Example for SAR 25 goods return, no tax and no previous credits:

| Applied customer receipts | Invoice open AR | Indicative AR reduction | Indicative refund obligation | Classification |
|---:|---:|---:|---:|---|
| 0 | 25 | 25 | 0 | internal_credit_ready |
| 10 | 15 | 15 | 10 | requires_refund_liability_workflow |
| 25 | 0 | 0 | 25 | requires_refund_liability_workflow |

If the RMA has already been internally credited, neither a new AR reduction nor a new refund is proposed. The report does not create a payable or bank transfer. Unsettled invoice balances are *snapshots*, not authorization to send money. A production workflow must recheck under the original-invoice lock.

A VAT-rated original invoice is explicitly classified \`requires_zatca_tax_pack\`; any tax shown is **only arithmetic from the original line**, not a legally approved tax amount. For partial billed lines/discounts/multi-currency original sources, fail closed. FX conversions and matching multi-invoice credit notes are out of scope.

## 035: Saudi-standard-rated goods credit source consistency

Pure Python module \`src/one_erp_core/ksa_credit_source_policy.py\` provides
\`assess_ksa_standard_goods_credit_source()\` and does not post anything.

For a bounded proof case (one SAR line, original and credit standard-rated
15% VAT, same price/tax category/code, no discount), it checks immutable trusted:

- original customer tax invoice number and exact invoice UUID; credit note source reference;
- source issuer legal/VAT identity, registered ZATCA status and buyer legal identity;
- standard invoice *clearance* vs simplified invoice *reporting* route;
- accepted goods-return source ID, quantity and earlier credited source quantity;
- return reason, invoice chronology, company/customer, approved source VAT code;
- credit base, output-VAT reversal arithmetic and gross credit totals.

**This is only a fail-closed source-consistency check.** Do not interpret its result as ready for invoice output or ZATCA submission. Exact validation depends on the applicable regulations, XML implementation rules, controlled numbering (ICV), PIH, hash/signature, seller EGS identity, additional buyer/seller information, QR/security, compliance/certificates, source tax-period treatment, clearance or reporting acknowledgements, valid human authorizations, archived signed documents and externally verified original invoice registry.

Official references:
- ZATCA E-invoicing overview: https://www.zatca.gov.sa/en/E-Invoicing/Pages/default.aspx
- ZATCA Electronic Invoice XML Implementation Standard (official): https://www.zatca.gov.sa/ar/E-Invoicing/SystemsDevelopers/Documents/20220624_ZATCA_Electronic_Invoice_XML_Implementation_Standard_vF.pdf
- ZATCA detailed invoicing guidelines, Credit/Debit notes section 4.3: https://www.zatca.gov.sa/en/e-invoicing/introduction/guidelines/documents/e-invoicing_detailed__guideline.pdf
- ZATCA business and technical specifications: https://www.zatca.gov.sa/en/E-Invoicing/SystemsDevelopers/Pages/E-Invoice-specifications.aspx

## Legal accounting release gates before real money or e-documents

1. Signed actor / physically isolated tenant DB / minimum permissions and approved tax policy per establishment; user-supplied SQL context or source evidence is never authoritative.
2. Review and approve which invoice type, VAT category, VAT country/functional currency, original invoice VAT period, required human approval and legal reason apply.
3. Implement compliant credit document XML generation, reference, sequential legal numbering, signing, QR and ZATCA integration with official acceptance/testing. Do not infer approval from pure validator.
4. Split invoice settlement into AR reduction and legally supported customer refund liability, account for prior applied cash and payment gateway fees; never credit AR into a negative balance as a disguised cash refund.
5. Implement separate approved refund order, payment matching, bank statement reconciliation, retries, exceptions and reversal under immutable audit logs.
6. Reconcile each customer RMA across warehouse cost/QA/NRV, posted revenue/AR/VAT, signed legal credit, cash refund, close, and IFRS disclosures.

All existing migrations and new tests are **disposable PostgreSQL CI only**, no production migration or Oracle modification.
