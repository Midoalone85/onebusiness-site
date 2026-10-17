# ONE ERP NEXT | Verified checkpoint and remaining delivery work

Checkpoint: 2026-10-15 after independently tested migration 043.
Project: standalone ONE ERP NEXT; do not equate it with the Oracle-hosted
legacy service or the static public one-erp-preview demonstration.

**Actual source position:** independent feature branch
\`feature/one-erp-smart-finance-20261015\`; draft PR #20.
**CI evidence:** 464/464 tests passed under EACH of Python 3.11 and
Python 3.13 with disposable PostgreSQL 14, run 38742763693. These tests
do not prove production readiness or legal compliance.

## Work completed or substantially prototyped in isolated tests

| Area | Available development evidence | Status |
| --- | --- | --- |
| Ledger and internal financial docs | Double entry, accounting periods, reversal lineage, invoice/payment and receivable aging/report-policy primitives, append-only source audit | Tested development core; not a complete corporate accounting close |
| Sales/purchases and inventory | Commercial orders, source-backed physical receipts, reservation, delivery, fulfillment, returns and valuation, GRNI, gross COGS | Transactionally tested for strict pilot cases |
| Valuation and inventory impairment | IAS 2 NRV write-down/reversal, separate contra inventory allowance, sale allocation and return/quarantine revaluation | Narrow tested pilot, not all accounting policy elections |
| Supplier returns | Billed/unbilled goods return paths, matching source receipt/bill and GRNI/AP impact | Narrow tax-free, homogeneous-cost single-source pilot |
| Customer returns and credits | QA, cost/stock trace, internal credit/AR/revenue reversal and prior-payment exposure | Tax-free, source-limited internal accounting pilot |
| Customer refund-payable | Split AR vs refund liability, approval separation, immutable audit, tested replay/concurrency | Internally recognized, bank and legal credits unfinished |
| Bank candidate and ONE PULSE | Manually supplied *unverified* bank debit references, duplicate prevention, internal stage dashboard | Not actual bank statement ingestion or payment |
| Accounting period close refund check | 043 blocks draft journals / refund discrepancies, captures immutable close source figures | Additional refund-focused close gate, not audited year-end close |
| Identity/control-plane code | RS256/OIDC verification building blocks, tenant entitlements and provisioning constraints, read-only report roles | Local/CI prototypes only, not proven production IdP/tenant hosting |
| Arabic-English demonstration UI | Static bilingual preview with fictional data | Not connected to independent financial backend |

### Verified 043 detail
\`db/043_refund_period_close_gate.sql\` implements a refund-control
preflight, blocks close if draft journals or internal refund GL/source
control anomalies remain, and records a read-only immutable close snapshot
using \`one_period_refund_close_evidence\`. A correctly posted, still
outstanding customer refund liability **does not block closing**.
The check is not a complete inventory/bank/tax/consolidation year-end
close, and the record is NOT a signed external auditor's statement.

## Release blockers, ordered by dependency (not by desire)

### P0: Security and production foundations

1. Independently verified live OIDC/SSO IdP, MFA / recovery / token
   revocation and production key rotation. Verify no browser/user-supplied
   SQL actor GUC can impersonate another tenant or financial approver.
2. Physically separate tenant database, signed control-plane entitlement,
   zero raw SQL access to customers, least-privilege runtime roles,
   audited tenant creation/suspension/deletion, tenant-aware migration
   and strict secrets handling.
3. CI -> independently backed-up Oracle **staging** -> controlled production
   release, verified HTTPS/DNS, scheduled backup and **tested restore**,
   application logs/alerts, threat model, penetration and load tests.
4. End-to-end authenticated HTTP transaction routes (not only read-only
   dashboard primitives), isolated tenant connection pooling, idempotent
   retries, failure/restart processing and actual full browser tests.
5. Connect real Arabic RTL/English LTR production UI and ONE PULSE to
   these secured routes. Remove invented financial data and ensure menus
   navigate to independent module pages without unbounded panels.

### P0: Minimum usable trade/finance ERP

6. Unified customer/supplier/person records and National Address, pricing
   and sales/purchase workflows, multi-line invoices and orders, shipping
   exception handling, discounts/retentions, returns/credit allocations.
7. Inventory lots, batches, serial numbers, expiry, units/packaging,
   locations/bins, warehouse transfers, negative-stock policy, opening
   balances, cost layers and a selected documented FIFO/weighted-average/
   specific-ID policy, and landed-cost/variance adjustments.
8. Real posting ownership and reconciliation of PO -> receipt -> inventory
   -> GRNI -> vendor bill/AP -> settlement; sales delivery -> COGS ->
   invoicing/AR -> receipt -> return/refund. Block legacy non-valued paths.
9. Bank feeds, trustworthy statement origin and customer/vendor payment
   evidence, settlement clearing, failed/rejected transfers, fees, cash
   control, debtor/creditor statements, period-end balances and approvals.
10. IFRS/SOCPA-tailored entity accounting policies, trial balance, P&L,
    financial position, cash flow, equity movement, comparative notes,
    provisions (EOSB/ECL etc.), profit/expense allocations and
    FX/functional-currency rules. No universal compliance claim.

### P0: KSA local statutory gate (must pass before live KSA invoices)

11. Approved ZATCA tax pack, 15% and exceptional VAT category/recoverability
    logic, lawful corrections and credit/debit tax documents, seller/buyer
    details, ZATCA-compliant UBL/XML, protected sequence/ICV/PIH/QR/signing,
    clearance or reporting by legally relevant invoice type and phase,
    compliant archival, official sandbox evidence and tax-period handling.
12. VAT, WHT, Zakat/income-tax reports only after mapped local law and
    official filing reconciliation. Country packs for Egypt/UAE/India/Sudan
    have their own accounting/tax/currency rules and must never inherit
    Saudi settings by accident.

### P1: SaaS commercialization and broader modules

13. Real seven-day trial onboarding with automatic isolated tenant DB
    creation and termination/suspension rules; paid plan pricing, verified
    gateway token/authorization/capture, correct invoices, renewal and
    payment-provider webhooks. Never convert a trial to paid without verified
    captured settlement.
14. CRM pipeline, POS/cashier, direct expenses and approval, HR, payroll,
    WPS and end of service, manufacturing BOM/WIP/work orders, projects,
    service contracts, repairs and maintenance, mobile applications,
    reporting/exports. Each needs secured live module operations, not only
    icons or static cards.
15. Ask ONE finance AI with authenticated scoped read access, traceable
    citations to source journal/period/company and explicit human approval
    for every possible write/action. No autonomous posting or hallucinated KPI.

### P2: Industry-specific release tracks

16. Hospital/clinics: patient/encounter, appointment/billing, consent/privacy,
    wards/beds/diet, nursing/MAR, clinical orders, lab and radiology,
    insurance claims, pharmacy batch/expiry/cold-chain; jurisdictional
    certification and healthcare-safety review before real patient data.
17. Manufacturing: routing, MRP, capacity, production BOM/WIP/variance,
    batch trace; services/maintenance: technician work orders, SLA, spare
    parts, field service, subscriptions and warranty.
18. Other vertical templates are onboarding profiles only until their
    actual workflows, schema, privacy and QA have been delivered.

## Ship decisions

- **Green for research / engineering iteration:** disposable PostgreSQL CI,
  source-linked pilot logic, paper-based rule acceptance and static demos.
- **Not green for users, money, statutory documents, patients or production:**
  verified IdP, commercial production UI, bank integration, ZATCA, robust
  approval/tenant security, external test environment, backup recovery and
  independent accounting/security review still have no evidence.
- No reliable percent-complete estimate is possible from migration count
  or regression-test count alone. Track *gates passed with evidence*, not
  numbers of files or elapsed chat messages.
- Do not auto-merge the draft PR or deploy these migrations to Oracle.
  Production approval requires executable evidence for EACH P0 gate.

## Immediate engineering priority

Start with the production identity -> least-privilege per-tenant database ->
HTTPS API and live connected sales/stock/finance workspaces. In parallel
build country-reviewed tax/credit document rules and authenticated statement
integration. Then run a complete end-to-end synthetic purchase-to-cash
trial in Oracle staging, reconcile balance sheet and confirm recovery.
Only after those controls can a real pilot begin.
