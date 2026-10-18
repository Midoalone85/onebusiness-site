# ADR: One logical core; defer horizontal autoscaling until after launch

**Date:** 2026-10-18
**Status:** ACCEPTED DESIGN DECISION, NOT DEPLOYED OR VERIFIED IN PRODUCTION
**Applies to:** ONE ERP NEXT independent finance and operating platform
**Current delivery:** one deliberately provisioned application deployment; no automatic scaling
**Decision owner:** ONE ERP product leadership

## Decision (قرارنا)

ONE ERP NEXT will retain **one logical platform core** with independently bounded business modules, rather than seven unrelated cores. Modular boundaries permit moving an expensive worker/service out later, without forking accounting truths or entitlements.

**Automatic horizontal scaling is a post-commercial-launch enhancement**, not a condition for the first commercial release. Do NOT provision Oracle autoscaling pools, extra replicas, managed queues or additional databases just to claim this ADR was implemented. Scaling is not evidence of product readiness.

The platform core must remain the authority for tenant identity, permissions, policy version, module contracts and audit boundaries. The Finance ledger remains the single accounting source of truth, with posting, reversals and source-linkage protected by database transactions and durable idempotency.

## Do NOW (low-cost design constraints, not extra infrastructure)

1. Keep services as **stateless as reasonably practical**: do not rely on in-process user sessions, per-instance authorization grants, sequence counters, lock flags, document approvals, financial totals or tenant selections as authoritative state. A WSGI object may hold trusted immutable configuration and a tenant-to-factory registry; it must not cache a user's granted permissions across requests.
2. All authorizations are rechecked against trusted identity and current tenant membership, **before** opening any tenant database. Preserve the dedicated-database-per-tenant production plan. Deny unknown, stale, revoked or cross-company requests.
3. Every money-impacting command must use server-derived actor/tenant identity, an enforced unique durable idempotency key, appropriate transaction/locking semantics and append-only audit entries. A retry after timeout must return the original committed result or a safe conflict, never post twice. Keep maker/checker separation.
4. All invoice/stock/payment interactions must define their **atomicity boundary**. Never promise distributed exactly-once execution from an HTTP or message queue alone. If modules later become asynchronous, use a transactional outbox, idempotent consumers, durable correlation IDs and explicit compensation/reconciliation for multi-step workflows.
5. Keep reporting read-only, no-store and tenant-scoped. Caches must be keyed by company, role/policy version and relevant as-of/snapshot; they must never be a source of financial truth. Prefer no shared confidential cache in initial launch.
6. DB connections are a shared bottleneck: enforce per-process pool limits, per-tenant budgets and upper bounds under the planned maximum replica count; separate privileged readers/writers and respect PostgreSQL max_connections. Adding application replicas does not horizontally scale a single write-primary database.
7. Health and readiness must represent separately evidenced TLS, trusted IdP, isolated tenant routing, restricted SQL roles, approved migrations and backup restoration. /healthz is liveness, **not** evidence of financial safety. Do not force /readyz green using hardcoded all-true probes.
8. No secrets, tokens, protected financial records, customer identity or private SQL connections in GitHub Pages, client storage, logs, metrics labels or public endpoints.
9. Keep expensive periodic jobs owned by **one** scheduler/lease or protected with distributed database locks and idempotency before multiple workers are ever enabled. Audit event persistence must be durable and actor-scoped, not a local timestamp display only.
10. Build measurable, reversible changes with an operational rollback path; protect developer CI from being misrepresented as live Oracle staging, banking, ZATCA, security certification or commercial readiness.

## NOT in first release

- No Kubernetes requirement, autoscaling groups, replica-count switch, automatic failover, distributed cache, managed broker or new Oracle spending merely for scale.
- No claim that multi-worker correctness, real database concurrency, login, banking, tax and physical tenant isolation are proven by this document or by unit tests.
- No direct AI-controlled posting or changes to permissions/periods without the existing human-approval policy.

## Explicit post-launch gates: ALL must pass before more than one application replica

**Gate A | Security and production readiness:** independent Oracle staging/prod approvals, real OIDC/MFA, per-tenant dedicated DB access control, signed permission revalidation, encrypted/managed secrets, audit retention, and restore drill.

**Gate B | State and jobs:** no sticky local auth state or single-instance local file dependence; revoked sessions propagate across instances; job scheduler/lease and exactly-one financial effects are tested.

**Gate C | Financial correctness:** race/load cases prove concurrent invoice issuance, payment allocation, stock reservations, closing/late adjustments, returns and retries preserve invariants and audit. Include partial failure, dropped connection, restart during commit, and cross-tenant denial tests on actual PostgreSQL with restricted production-like roles.

**Gate D | Operations:** trusted reverse proxy/TLS, readiness/liveness separation, graceful shutdown/draining, request timeouts, worker/pool limits, standardized telemetry with NO credentials, backup/restore verification, known rollback version and migration compatibility.

**Gate E | Capacity and price:** reproduce representative loads per tenant, measure p95/p99 latency, DB CPU/IO and locks, queue lag, error rate and connection saturation. Require a fixed monthly budget ceiling, deployment min/max and approval before any cost-raising expansion.

**Gate F | Release controls:** human signoff, shadow/canary validation, rollback rehearsed, audit trail, no unreviewed migrations. If any gate fails, keep the original one-deployment topology.

## Post-launch implementation sequence

1. Baseline and load-test one application instance; identify DB/CPU/IO/query bottlenecks first.
2. Test two isolated **application** instances behind a trusted load balancer in a disposable staging environment with identical reviewed code and secrets configured out of band; confirm stateless sessions, secure routing, drains and retry semantics.
3. Move recurring jobs to a single coordinated durable executor if needed; put durable outbox/consumer mechanics only around workflows that actually require them.
4. Tune PostgreSQL connection budgets and isolate read replicas only where financial snapshot consistency is preserved. A write-primary remains the accounting truth.
5. Enable bounded Oracle autoscaling only when measured demand justifies it; alert on cost/availability and maintain a one-instance rollback topology.

## Traceable present evidence and known gaps

- `src/one_erp_core/staging_router.py`: trusted verifier/tenant registries and denial until independently reported staging readiness. Actual external provider evidence is not wired here.
- `src/one_erp_core/staging_health.py`: HTTPS host-pinned /healthz and fail-closed /readyz. Six checks are probe **inputs**, not an Oracle certification.
- `src/one_erp_core/finance_wsgi.py`: trust-derived identity and tenant-scoped registry; no wildcard tenant database. This does not implement a real IdP.
- `tests/test_staging_router.py`: application-boundary unit tests, including independent-router negative tenant/access readiness checks; they are NOT load/stress/DB concurrency acceptance.
- Current PR stack is draft and has not been merged into main; CI on a feature branch is non-production evidence only.

### Arabic summary

**النواة منطقياً واحدة والوحدات متخصصة. نجهّز الآن حدود الهوية والصلاحيات والمعاملات وعزل الشركات، ونؤجل تشغيل نسخ متعددة والتوسع التلقائي لما بعد الإطلاق التجاري وبعد اختبارات الضغط والأمان والتكلفة. لا نغيّر خادم Oracle ولا نشغّل خدمات مدفوعة بموجب هذا القرار.**
