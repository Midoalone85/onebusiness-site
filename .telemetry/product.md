# ONE ERP NEXT - Product Tracking Model
Status: Proposed model, not yet instrumented
Source: Product requirements discussed with the project owner. Not generated from a full application code audit.
Last updated: 2026-10-17

## Product
ONE ERP NEXT is a multi-tenant, configurable B2B ERP/SaaS platform supporting Arabic RTL and English LTR. The vision includes accounting, sales, purchases, inventory, manufacturing, POS, CRM, HR/payroll, projects, expenses, industry packages (healthcare, trade, maintenance, factories), AI insights, and subscriptions.

## Actors
- SaaS platform administrator: tenant provisioning, billing, support, operational health.
- Tenant owner: configuration, subscription, adoption and business outcomes.
- Finance manager / accountant: GL, payments, tax, reconciliation, close and financial reporting.
- Sales / purchasing / inventory / operations employees: operational transaction journeys.
- Department approver / auditor: approvals and traceability.

## Primary product entities
tenant_id (pseudonymous), company_id, branch_id, user_id (pseudonymous), role_key, module_key, subscription_plan, trial_id, invoice_id (opaque ID), sales_order_id (opaque ID), purchase_order_id (opaque ID), stock_move_id (opaque ID), journal_entry_id (opaque ID), ai_session_id (opaque ID).
Use stable opaque IDs only; never send invoice body, customer identity, national IDs, banking information, credentials, salary, health records or free-text prompts.

## Core value journeys
1. Tenant onboarding: tenant_created -> trial_started -> admin_invited -> initial_configuration_completed -> first_module_opened.
2. Finance: chart_of_accounts_configured -> journal_posted -> trial_balance_viewed -> period_closed -> report_exported.
3. Sales to cash: quotation_created -> sales_order_confirmed -> delivery_completed -> invoice_posted -> payment_registered.
4. Procure to pay: purchase_order_confirmed -> goods_received -> vendor_bill_posted -> vendor_payment_registered.
5. Inventory: product_created -> stock_receipt_validated -> stock_transfer_validated -> stock_valuation_viewed.
6. AI guidance: ai_assistant_opened -> ai_query_submitted (metadata only) -> ai_answer_delivered -> ai_action_accepted.
7. Monetization: pricing_viewed -> plan_selected -> checkout_started -> payment_confirmed -> subscription_activated/renewed (server-side truth).

## Event envelope
event_name, event_version, occurred_at (UTC), tenant_id, company_id, branch_id (optional), actor_id, actor_role, session_id, module_key, object_type, object_id (opaque), result (success/error), error_code (safe enumerated), duration_ms, environment, release_version.
Recommended: request_id for deduplication, explicit consent/category where applicable.

## Priority questions / KPIs
- Trial activation: percentage of trial tenants completing configuration and posting their first real transaction.
- Time-to-first-value: median duration from tenant creation to first posted transaction.
- Feature adoption: weekly active tenants and unique users by module.
- Workflow completion: order -> delivery -> invoice -> payment; purchase -> receipt -> bill.
- Failure rate / latency: failed critical actions and p95 duration.
- AI effectiveness: percent of sessions with a confirmed useful action; avoid inferring satisfaction from answer generation alone.
- Trial to paid conversion: actual payment-confirmed subscriptions, not UI status flags.
- Retention: active tenants with completed business workflows at 7/30/90 days.

## Privacy and tenant safety
- Track application metadata only; exclude all PII, financial amounts unless explicitly approved and aggregated, confidential journal contents, credentials, payment tokens and medical data.
- Apply tenant isolation in analytics ingestion and queries. Apply role-level access control to dashboards.
- Server-side authoritative events for payment, posting, provisioning and security-relevant actions.
- Observe applicable Saudi PDPL and other jurisdictional requirements before enabling production analytics; retention, consent/legal basis and DPA must be reviewed.
- Disable telemetry by default until reviewed. Use a separate test environment and synthetic sample data.
- Do not modify transaction logic or IFRS/SOCPA/ZATCA outcomes to emit analytics.

## Verification checklist
[ ] Confirm this is the actual ERP source repository (the currently connected repo appears to host the site/preview).
[ ] Locate app frontend/backend and existing analytics SDK, if any.
[ ] Validate event definitions against actual routes and domain model.
[ ] Select analytics destination and credentials through secure configuration.
[ ] Implement typed event schema and automated tests on a feature branch.
[ ] Check opt-outs, retention, tenant-level restrictions and data minimization.
[ ] Verify test events end-to-end without real customer data.
[ ] Enable production instrumentation only after a separate review.
