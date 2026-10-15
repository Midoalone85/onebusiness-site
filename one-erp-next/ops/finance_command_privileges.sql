-- ONE ERP NEXT / 053 | DBA REVIEWED restricted finance-command runtime
-- Apply ONLY after migrations 001..051 in each isolated tenant PostgreSQL DB.
-- Do not run on shared production before DBA signoff and migrations finalized.
-- No LOGIN/password granted here; create a separate secret-managed LOGIN for
-- backend WSGI workers only and explicitly GRANT this NOLOGIN role to it.
--
-- These functions are SECURITY INVOKER and perform row-level authorization
-- via one.tenant_id / one.actor_id, set by trusted backend from verified OIDC.
-- This role CANNOT prevent spoofing if raw arbitrary SQL is exposed! Some
-- other legacy functions also retain PUBLIC EXECUTE defaults; perform a
-- separately reviewed PUBLIC grants audit before any production deployment.
BEGIN;
DO $ddl$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_roles
  WHERE rolname='one_erp_finance_command_executor') THEN
  CREATE ROLE one_erp_finance_command_executor
   NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
   NOREPLICATION NOBYPASSRLS;
 END IF;
END $ddl$;

-- PostgreSQL 14's default public schema may grant CREATE to PUBLIC.
-- PostgreSQL cannot revoke an inherited PUBLIC permission for only one role.
-- Deny it to PUBLIC at the dedicated tenant database level. A DBA must
-- verify other legitimate schema authors have explicit GRANT CREATE.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- This script does not grant role membership, broad schema DDL or DB LOGIN.
GRANT USAGE ON SCHEMA public TO one_erp_finance_command_executor;
REVOKE ALL ON ALL TABLES IN SCHEMA public
 FROM one_erp_finance_command_executor;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public
 FROM one_erp_finance_command_executor;

-- Membership and 048 permissions: necessary for invoker validation.
-- Accounting period, chart, GL, currency, and reviewed adjustment facts.
GRANT SELECT ON TABLE
 one_memberships,
 one_permission_templates,
 one_permission_change_requests,
 one_permission_change_approvals,
 one_periods,
 one_currency_precision,
 one_accounts,
 one_journal_entries,
 one_journal_lines,
 one_period_soft_close_requests,
 one_period_soft_close_approvals,
 one_soft_adjustment_requests,
 one_soft_adjustment_approvals,
 one_soft_adjustment_post_audit,
 one_soft_adjust_default_limits,
 one_soft_adjust_limit_requests,
 one_soft_adjust_limit_approvals,
 one_soft_adjustment_approval_evidence,
 one_soft_adjustment_statement_approval_seals
TO one_erp_finance_command_executor;

-- No updates/deletes of user memberships, other business modules, posted GL
-- or audit records. Allowed writes are only those made by vetted stored
-- routines in 049..052; immutable triggers additionally enforce the workflow.
GRANT INSERT ON TABLE
 one_period_soft_close_requests,
 one_period_soft_close_approvals,
 one_soft_adjustment_requests,
 one_soft_adjustment_approvals,
 one_soft_adjustment_post_audit,
 one_soft_adjustment_approval_evidence,
 one_soft_adjustment_statement_approval_seals,
 one_journal_entries,
 one_journal_lines,
 one_ledger_audit,
 one_operation_outbox
TO one_erp_finance_command_executor;

-- Journal-posted audit fanout writes to one_operation_outbox through
-- one_route_operation_event trigger. INSERT is necessary for committed GL;
-- there is deliberately NO SELECT/UPDATE/DELETE on outbox for this role.

-- The INVOKER function and line-edit trigger use SELECT .. FOR UPDATE.
-- These column-scoped UPDATE privileges supply PostgreSQL's row lock right.
-- Triggers block ordinary updates to evidence/request rows and enforce
-- period closure and ledger posting guards on guarded columns.
GRANT UPDATE(closed) ON one_periods TO one_erp_finance_command_executor;
GRANT UPDATE(period_id) ON one_period_soft_close_requests
 TO one_erp_finance_command_executor;
GRANT UPDATE(id) ON one_soft_adjustment_requests
 TO one_erp_finance_command_executor;
GRANT UPDATE(status,posted_at,posted_by) ON one_journal_entries
 TO one_erp_finance_command_executor;

-- 049-052 PUBLIC EXECUTE was revoked for listed entrypoints. Grant only
-- dependencies actually called from the 052 safe command operations.
GRANT EXECUTE ON FUNCTION
 one_actor_has_permission(uuid,uuid,text),
 one_actor_soft_adjust_limit(uuid,uuid,text),
 one_request_period_soft_close(uuid,uuid,uuid),
 one_approve_period_soft_close(uuid,uuid,uuid),
 one_request_soft_adjustment_lines(
  uuid,uuid,uuid,uuid,date,text,jsonb,text,uuid),
 one_approve_soft_adjustment(uuid,uuid,uuid),
 one_post_journal(uuid,uuid,uuid),
 one_soft_adjustment_statement_comparison(uuid,uuid,uuid)
TO one_erp_finance_command_executor;

-- Defense in depth: explicit denial of known unrelated financial actions.
-- PUBLIC EXECUTE and other inherited grants must be audited separately.
REVOKE ALL ON FUNCTION
 one_close_period(uuid,uuid,uuid),
 one_issue_invoice(uuid,uuid,uuid),
 one_apply_payment(uuid,uuid,uuid),
 one_request_permission_change(
  uuid,uuid,uuid,text,text,text,date,date,uuid),
 one_approve_permission_change(uuid,uuid,uuid),
 one_request_soft_adjust_limit(
  uuid,uuid,uuid,text,numeric,date,date,uuid),
 one_approve_soft_adjust_limit(uuid,uuid,uuid)
FROM one_erp_finance_command_executor;
COMMIT;

-- Before production: test `has_function_privilege` incl. PUBLIC grants
-- and any role memberships; never confuse a REVOKE from this role with
-- removal of independently-granted PUBLIC access.
-- Hardening requirements: dedicated trusted application SQL service,
-- schema-qualified function calls, controlled search_path, TLS, per-tenant
-- database separation, connection pool role assertions and credential vault.
