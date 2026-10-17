# ONE ERP NEXT | Internal Invoice Maker-Checker Backend | 17 Oct 2026

**Status:** development-only branch, draft PR #47. No Oracle deployment and no approval user interface is available on a live host.

## What is implemented

- `invoice_source_workflow.py` adds `create_trusted_invoice_source`: verifies server-authenticated actor/tenant, checks current DB finance membership, calls a trusted backend-only `TrustedInvoiceSourceBuilder`, validates the canonical invoice snapshot, verifies an open accounting period, and inserts an immutable source request under migration 059. The source builder is intentionally an **interface**: a reviewed commercial-order-to-invoice adapter for real sales workflows is still required. It is not safe to connect it to browser-provided amounts or chart mappings.
- `independently_approve_invoice_source`: takes only a document UUID and tenant UUID, rechecks active finance membership, verifies maker and reviewer differ, and creates a unique immutable approved source record. Database triggers enforce append-only fields and prevent self-review.
- `invoice_approval_wsgi.py`: a standalone signed bearer, exact HTTPS host, strict POST JSON approval route. No browser-provided amount, currency, actor, period, bank or SQL input; no cookies, queries or unofficial transports. It uses an independently configured tenant-to-approval-connection registry and returns a reference only. Duplicate/source-missing cases never expose database errors.
- `staging_router.py` can mount the approval handler **only with explicit `enable_invoice_source_approvals=True` and a fourth dedicated `approval_connections` registry**. The route is **off by default** and additionally gated behind the existing six trusted readiness checks.
- Real disposable PostgreSQL14 tests cover independent reviewer signing through the HTTP handler, refusal of self-approval/revoked roles, trusted source creation, invalid exact-decimal inputs, unavailable accounting periods, single posting and replay. No customer data is used.

## Important remaining work

1. **Actual trusted invoice source builder**: transform persisted dispatched sales, customer, lines, account mapping and local tax decisions into one frozen snapshot with source event references and approvals. The current backend interface is not an implementation of that business adapter.
2. **Separate production SQL roles** for source preparation, maker, approval, posting and reporting. The Python registries must be backed by distinct restricted database logins; actual roles and inherited grants require independent review.
3. External authenticated OIDC provider, verified current memberships, role assignment/audit, reviewer UX, evidence attachments and cancellation/escalation policy. Never rely on client-controlled actor IDs or raw SQL access.
4. Do not treat internal invoice posting as legally valid ZATCA issuance. Official Saudi VAT/tax rules, UBL, QR, signature, clearance/reporting and certified integrations are separate milestones.
5. Tenant provisioning to dedicated Oracle-hosted staging databases, independent backup/restore validation, rollback rehearsal and full browser E2E acceptance.
6. Payments remain **denied in the existing approved source loader**. Bank and SaaS billing require external trusted payment evidence and separate maker/checker workflow.

## Safety and release

- No SQL migrations changed in this feature PR; the new code uses migration 059.
- No `main` merge, Oracle provisioning, production route, bank transaction, official invoice generation, mail/send or real subscription charge was performed.
- Continue strict draft PR stacking: PR #47 on PR #46 on PR #45 on PR #44 and earlier dependencies.
