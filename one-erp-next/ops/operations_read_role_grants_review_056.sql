-- ONE ERP NEXT 056 | PRODUCTION-REVIEW TEMPLATE, NOT CI migration.
-- Run only after security review in a PHYSICALLY ISOLATED TENANT DB.
-- Prerequisite: DBA provisioned a dedicated trusted backend login role
-- "one_erp_operations_reader", without SUPERUSER/BYPASSRLS/CREATEROLE,
-- schema ownership, arbitrary SQL access from browser, or inheritance
-- from finance posting roles. Authentication context is server-issued only.
-- Do not run on the existing shared CI fixture or live Oracle service.
BEGIN;
GRANT USAGE ON SCHEMA public TO one_erp_operations_reader;
GRANT SELECT ON
  one_memberships,
  one_permission_templates,
  one_permission_change_requests,
  one_permission_change_approvals,
  one_products,
  one_warehouses,
  one_stock_balances,
  one_commercial_orders,
  one_commercial_order_lines,
  one_return_jobs
 TO one_erp_operations_reader;
GRANT EXECUTE ON FUNCTION one_actor_has_permission(uuid,uuid,text)
 TO one_erp_operations_reader;
-- No INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER or function
-- EXECUTE permission to stock fulfillment, posting, invoice, or bank ops.
-- SQL injection prevention, network/private DB credentials and OIDC proof
-- must be enforced in the calling service, never by accepting arbitrary
-- SET one.tenant_id / one.actor_id commands from an end user.
COMMIT;

-- Post-deployment audit (DBA only, fail release on unexpected grants):
-- SELECT rolname,rolsuper,rolbypassrls,rolcreaterole,rolcreatedb
--   FROM pg_roles WHERE rolname='one_erp_operations_reader';
-- SELECT * FROM information_schema.role_table_grants
--   WHERE grantee='one_erp_operations_reader';
-- SELECT * FROM information_schema.role_routine_grants
--   WHERE grantee='one_erp_operations_reader';
