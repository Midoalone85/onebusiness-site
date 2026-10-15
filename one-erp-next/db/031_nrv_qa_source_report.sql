-- ONE ERP NEXT / 031 | QA source reconciliation; CI only.
BEGIN;
CREATE FUNCTION one_nrv_qa_return_check(p_tenant uuid,p_actor uuid,p_return uuid)
RETURNS TABLE(original_qty numeric,original_cost numeric,new_allowance numeric,
  qa_net_value numeric,source_reconciled boolean)
LANGUAGE plpgsql STABLE SECURITY INVOKER AS $$
BEGIN
 IF p_tenant IS NULL OR p_actor IS NULL OR p_return IS NULL
 OR current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text
 OR current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text
 OR NOT EXISTS(SELECT 1 FROM one_memberships m WHERE m.tenant_id=p_tenant
  AND m.actor_id=p_actor AND m.active
  AND m.role IN ('company_admin','accountant','viewer')) THEN
 RAISE EXCEPTION 'Authorized company member required';
 END IF;
 IF NOT EXISTS(SELECT 1 FROM one_return_jobs
  WHERE tenant_id=p_tenant AND id=p_return) THEN
 RAISE EXCEPTION 'Return case inaccessible';
 END IF;
 RETURN QUERY
 SELECT r.quantity,r.original_gross_return_cost,r.new_allowance,
   (v.book_value-n.allowance)::numeric,
   (j.status='completed' AND j.accepted_qty=r.quantity
    AND j.source_event_id=r.original_sold_event_id
    AND j.processed_event_id=r.physical_return_event_id
    AND s.quantity>=r.quantity
    AND ev.qty=r.quantity AND ev.amount=r.original_gross_return_cost
    AND ev.journal_id=r.gross_return_journal_id
    AND g.status='posted' AND k.status='posted'
    AND n.allowance=r.new_allowance
    AND b.on_hand=r.quantity AND v.qty=r.quantity
    AND v.book_value=r.original_gross_return_cost
    AND v.book_value-n.allowance=r.reassessed_nrv)::boolean
 FROM one_nrv_customer_return_reviews r
 JOIN one_return_jobs j ON j.tenant_id=r.tenant_id AND j.id=r.return_job_id
 JOIN one_inventory_nrv_sale_releases s ON s.tenant_id=r.tenant_id
  AND s.stock_event_id=r.original_sold_event_id
 JOIN one_stock_value_events ev ON ev.tenant_id=r.tenant_id
  AND ev.stock_event_id=r.physical_return_event_id
 JOIN one_journal_entries g ON g.tenant_id=r.tenant_id
  AND g.id=r.gross_return_journal_id
 JOIN one_journal_entries k ON k.tenant_id=r.tenant_id
  AND k.id=r.new_nrv_journal_id
 JOIN one_stock_balances b ON b.tenant_id=r.tenant_id
  AND b.warehouse_id=r.quarantine_warehouse_id
  AND b.product_id=(SELECT product_id FROM one_stock_events
    WHERE tenant_id=r.tenant_id AND id=r.physical_return_event_id)
 JOIN one_stock_value_positions v ON v.tenant_id=b.tenant_id
  AND v.warehouse_id=b.warehouse_id AND v.product_id=b.product_id
 JOIN one_inventory_nrv_positions n ON n.tenant_id=b.tenant_id
  AND n.warehouse_id=b.warehouse_id AND n.product_id=b.product_id
 WHERE r.tenant_id=p_tenant AND r.return_job_id=p_return;
END $$;
REVOKE ALL ON FUNCTION one_nrv_qa_return_check(uuid,uuid,uuid) FROM PUBLIC;
COMMIT;
