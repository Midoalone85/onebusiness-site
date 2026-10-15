-- ONE ERP NEXT / 054 | DBA-controlled PostgreSQL public-function EXECUTE lockdown.
-- This opt-in script MUST run AFTER migrations 001..051 and 053's
-- finance_command_privileges.sql, in a DEDICATED tenant database.
-- It removes PUBLIC EXECUTE from every public-schema function, not just
-- known finance entrypoints. Existing finance command executor explicit
-- grants remain intact. A single global REVOKE can break other app roles:
-- audit/report/maintenance service callers, regrant by review BEFORE use.
-- Do not deploy to Oracle/test shared environments without role audit.
BEGIN;
-- PostgreSQL grants EXECUTE on newly created FUNCTIONS to PUBLIC by default.
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC;

-- Affect newly created routines owned by the role applying this script.
-- Other owners / migration identities need their own DEFAULT PRIVILEGES
-- set individually. Production deployment must restrict function ownership.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
 REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

-- Fail closed if an unrelated function is STILL callable by the restricted
-- finance command role (including PUBLIC or other inherited role grants).
-- Only strict routines invoked directly or indirectly by 052 are allowlisted.
DO $finance_exec_check$
DECLARE bad_functions text;
BEGIN
 SELECT string_agg(p.oid::regprocedure::text,', ' ORDER BY p.oid::regprocedure::text)
 INTO bad_functions
 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
 WHERE n.nspname='public' AND
   has_function_privilege(
     'one_erp_finance_command_executor',p.oid,'EXECUTE')
   AND p.oid<>ALL(ARRAY[
     'public.one_actor_has_permission(uuid,uuid,text)'::regprocedure,
     'public.one_actor_soft_adjust_limit(uuid,uuid,text)'::regprocedure,
     'public.one_request_period_soft_close(uuid,uuid,uuid)'::regprocedure,
     'public.one_approve_period_soft_close(uuid,uuid,uuid)'::regprocedure,
     'public.one_request_soft_adjustment_lines(uuid,uuid,uuid,uuid,date,text,jsonb,text,uuid)'::regprocedure,
     'public.one_approve_soft_adjustment(uuid,uuid,uuid)'::regprocedure,
     'public.one_post_journal(uuid,uuid,uuid)'::regprocedure,
     'public.one_soft_adjustment_statement_comparison(uuid,uuid,uuid)'::regprocedure
   ]);
 IF bad_functions IS NOT NULL THEN
  RAISE EXCEPTION
    'Unexpected PostgreSQL finance executor function grants: %',
    bad_functions;
 END IF;
 IF EXISTS(
   SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
   CROSS JOIN LATERAL aclexplode(
     coalesce(p.proacl,acldefault('f',p.proowner))) x
   WHERE n.nspname='public'
     AND x.grantee=0 AND x.privilege_type='EXECUTE'
 ) THEN
  RAISE EXCEPTION 'Unexpected PUBLIC EXECUTE still available on a public-schema function';
 END IF;
END $finance_exec_check$;
COMMIT;

-- DBA preflight: enumerate who calls non-finance functions (dashboard,
-- service integrations, recurring jobs) and explicitly grant the smallest
-- required signature to those trusted backend roles. Avoid granting all
-- functions to any PRODUCTION runtime role.
-- Schema qualification and transaction-local search_path are enforced in 052
-- boundary by stage 054; this file alone is NOT complete app security.
