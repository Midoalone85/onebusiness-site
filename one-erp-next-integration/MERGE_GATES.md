# ONE ERP NEXT | Integration Gate Matrix
Date: 2026-10-17
Environment: integration branch only. No Oracle or production mutations.

## Baseline facts
- Main has separate UI, navigation, brand and Oracle bridge work.
- Finance 056 diverged from main (359 ahead, 19 behind at inspection).
- Finance draft #36 source matrix 543/543 passed on Python 3.11 and 3.13.
- Current integration branch starts from main; finance not yet integrated.
- The gate workflow intentionally fails finance-presence until source lands.

## Required phases
| Gate | Evidence | Must pass before merge |
|---|---|---|
| Main UI preservation | Navigation tests and brand checks | Yes |
| Finance source inventory | src accounting and PG integration suite | Yes |
| Migration audit | SQL schema order and permissions | Yes |
| Python 3.11 PG14 tests | GitHub Actions full suite | Yes |
| Python 3.13 PG14 tests | GitHub Actions full suite | Yes |
| Refund maker-checker | Negative and positive bank candidate tests | Yes |
| Concurrent posting | One winner, no double record | Yes |
| NRV period filter | Explicit boundary date assertions | Yes |
| Tenant cross-access | Negative isolation cases | Yes |
| Multi-company permission | User denies and source gates | Yes |
| UI and accounting agreement | API read-only snapshots, no fake data | Yes |
| Production Oracle rollout | Separate approved deploy checklist | Later |

## Critical warning
Do not disable the finance-presence gate just to make CI green. Do not
run destructive PostgreSQL fixture setup on any non-disposable DB. Do not
expose CI credentials, generic SQL execution or custom GUC impersonation.
Do not assume a green test on source branch equals integration verification.
