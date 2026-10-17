# Product analytics implementation plan

## Repository audit
This repository contains the ONE Business site plus a **fictional-data** ONE ERP preview in `one-erp-preview/`. It is not sufficient evidence of a production accounting backend. All instrumentation remains disabled.

## Phase 1: Definition (completed)
- Map actors, tenant/company/branch boundaries, and value journeys in `product.md`.
- Document the allowlisted event taxonomy in `events.json`.
- Prioritize metrics and strict data minimization.

## Phase 2: Validate real sources (pending)
- Locate authoritative ERP source repository and backend APIs.
- Validate every event against actual business state, distinguish preview clicks from posted ERP transactions.
- Confirm existing analytics SDK and the real trial/billing architecture.

## Phase 3: Safe implementation (pending)
- Add a single typed tracker with explicit allowlisted properties and no-op default.
- In the preview, use strictly local mock event inspection only, with no network calls, only on a separate development branch.
- In the ERP backend, use server-trusted completion events; never call an invoice "paid" based only on client-side UI status.
- Test tenant isolation and analytics authorization, deduplication, GDPR/PDPL/privacy compliance, and access control.

## Phase 4: Verification (pending)
- Unit tests for rejection of emails, phone numbers, credentials, free text and sensitive record fields.
- Contract tests for event naming and versioning.
- End-to-end synthetic tenant tests for onboarding, sales, billing, and AI user journeys.
- Review before deployment and separately enable production analytics.

## Recommended dashboards after real instrumentation
1. Trial activation and median time to first business transaction.
2. First-week module adoption.
3. Sales and purchases funnel completion and drop-off.
4. Critical failure codes and latency.
5. Verified trial-to-paid conversion and retention.

Note: this is a custom hand-authored plan. The requested Accoil skill was not available in connected tools and has not executed.
