-- Read ONLY grants for the independent ONE ERP operations workspaces.
-- Installed into isolated tenant database by trusted DBA, not web requests.
BEGIN;
DO $r$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='one_erp_operations_reader') THEN
  CREATE ROLE one_erp_operations_reader NOLOGIN
   NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION;
 END IF;
END $r$;
GRANT USAGE ON SCHEMA public TO one_erp_operations_reader;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM one_erp_operations_reader;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM one_erp_operations_reader;
GRANT SELECT ON TABLE
 one_tenants,one_memberships,
 one_commercial_orders,one_commercial_order_lines,
 one_warehouses,one_products,one_stock_balances,
 one_stock_value_positions,one_return_jobs,
 one_nrv_refund_claims,one_customer_refund_orders,
 one_refund_bank_debit_candidates
 TO one_erp_operations_reader;
-- Role never receives INSERT/UPDATE/DELETE or EXECUTE to posting operations.
-- PostgreSQL GUCs are NOT authentication: only a verified backend may log in.
REVOKE ALL ON FUNCTION one_close_period(uuid,uuid,uuid)
 FROM one_erp_operations_reader;
REVOKE ALL ON FUNCTION one_post_journal(uuid,uuid,uuid)
 FROM one_erp_operations_reader;
REVOKE ALL ON FUNCTION one_issue_invoice(uuid,uuid,uuid)
 FROM one_erp_operations_reader;
REVOKE ALL ON FUNCTION one_apply_payment(uuid,uuid,uuid)
 FROM one_erp_operations_reader;
COMMIT;
