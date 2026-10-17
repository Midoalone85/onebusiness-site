# Finance CI triage | 17 October 2026

Source workflow: https://github.com/Midoalone85/onebusiness-site/actions/runs/39008793956
Source commit: 5a08d89
Python: 3.11 and 3.13 both fail the same integration stage. Install, compile and PostgreSQL startup pass.

## Confirmed failing groups (2 assertions, 5 exceptions)

1. Bank candidate registration: five tests fail because `one_register_refund_bank_candidate` raises
   `Independent bank candidate must match approved refund` (line 32).
   Compare exact approved instruction tuple returned from
   `one_refund_approved_instruction_preview` with inputs supplied by
   `register_bank_candidate` (tests/test_postgres_integration.py around 6535).
   Verify refund ID, invoice, partner/beneficiary, currency, amount,
   independent approval evidence, external payment reference, and actor.
   Do not disable independent reconciliation or maker-checker controls.
2. Concurrent bank candidate submission expects one winner and one rejected duplicate,
   but both fail. Determine whether the same approved-instruction mismatch causes
   both to fail before locking. Retest after fixing #1; preserve uniqueness.
3. NRV period report test at line ~5241 expects no rows in the window
   `date.today()+1` through `date.today()+30`; actual was
   (69, 923.000, 37.500, 885.500). Verify whether
   `one_inventory_nrv_period_activity` applies the requested start/end
   filter to every aggregated result, and that test fixtures do not include
   future-dated rows. Never alter accounting totals to satisfy the test.

## Before merge

- Capture exact SQLSTATE, parameters (excluding secrets), and approved instruction
  for the bank registration failure.
- Add tests for valid registration and wrong reference, wrong beneficiary,
  wrong tenant, wrong amount, duplicate reference, simultaneous submissions.
- Assert both zero and non-zero date-window NRV reports with boundary dates.
- Rerun the full PostgreSQL suite on Python 3.11 and 3.13.
- No production or Oracle migration until CI is green.
- Audit log PostgreSQL ERROR lines can contain expected negative test cases;
  only test runner's FAIL/ERROR summary determines suite failure.

This document is a triage record, not a claim that the failures have been fixed.
