# ONE ERP NEXT | Bank statement / ledger batch candidate matching (NO bank integration)

**Date: 17 Oct 2026. Development-only safety control, no production action.**

The standalone \`bank_match_batch.py\` takes preselected immutable imported bank statement rows and posted internal ledger movements for one authorized tenant, then produces **candidate suggestions only**, never a payment, journal, bank verification, refund release or reconciliation commit.

## Conservative guarantees

- Strict one-company records, bounded batch sizes, unique bank-line and ledger IDs and exact nonzero Decimal amounts (no float), with maximum three decimal places and exact currency matching. No exchange rate or guessed charge adjustments.
- Only posted, unreconciled journal movements within a maximum 31-day date window can be proposed. Candidate scoring inherits existing \`reconciliation.py\` logic.
- If a bank line might match multiple ledgers, it is reported as **ambiguous**. If multiple bank lines might match the same ledger, the candidate is explicitly flagged as a **collision**. The algorithm checks all eligible ledger matches, including those outside the top-25 display candidates.
- Each outcome has \`requires_independent_review=True\` and the batch states \`statement_evidence="import_unverified"\`, \`bank_settlement_verified=False\`, \`writes_performed=False\`.
- No receipt bank status is inferred from a statement row provided by a user or file import. A duplicated memo/reference alone is not sufficient to prove duplication or payment settlement, so identity is based on statement line IDs and a reviewed source.

## Real bank-go-live gates (still entirely pending)

1. Verified ownership of bank accounts, trusted provider/signed statement authenticity and statement line provenance.
2. Idempotent (bank + statement + line) uniqueness constraints in a tenant-isolated bank evidence registry, with approved replay and immutable audit trail.
3. Enforced one-to-one ledger binding with actual DB locks and reversible adjustment workflow, controlled maker/checker permissions and independent posting validation.
4. Reconciliation of clearing accounts and foreign exchange fees according to reviewed IFRS/SOCPA policy, plus complete exceptions UI.
5. Provider-side confirmation of actual funds, webhook signatures, retries/reversals/chargebacks and tenant entitlement, and a real Oracle Staging signed integration test.
6. Security, finance, DBA, bank, and human business approval before enabling ANY production transaction.

Passing local/CI unit tests is only evidence that this conservative matching algorithm behaves as specified. **This is not a bank connection, a bank balance, verified cash, or a completed settlement module.**
