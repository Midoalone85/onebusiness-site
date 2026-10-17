-- ONE ERP Next 020 | Atomic QA decision plus historical valued stock.
-- Requires 019: only source-costed customer returns qualify. PG14 CI only.
BEGIN;
CREATE TABLE one_return_disposition_costs (
 tenant_id uuid NOT NULL,
 disposition_id uuid NOT NULL,
 return_job_id uuid NOT NULL,
 quantity numeric(20,3) NOT NULL CHECK(quantity>0),
 amount numeric(20,3) NOT NULL CHECK(amount>0),
 action text NOT NULL CHECK(action IN ('restock','dispose')),
 journal_id uuid,
 actor_id uuid NOT NULL,
 created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 PRIMARY KEY(tenant_id,disposition_id),
 UNIQUE(tenant_id,journal_id),
 FOREIGN KEY(tenant_id,disposition_id)
  REFERENCES one_return_dispositions(tenant_id,id),
 FOREIGN KEY(tenant_id,return_job_id)
  REFERENCES one_customer_return_cost_links(tenant_id,return_job_id),
 FOREIGN KEY(tenant_id,journal_id)
  REFERENCES one_journal_entries(tenant_id,id),
 CHECK((action='dispose' AND journal_id IS NOT NULL)
   OR (action='restock' AND journal_id IS NULL))
);
CREATE INDEX one_return_disposition_cost_by_job
 ON one_return_disposition_costs(tenant_id,return_job_id);
ALTER TABLE one_return_disposition_costs ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_return_disposition_costs FORCE ROW LEVEL SECURITY;
CREATE POLICY one_return_disposition_cost_tenant ON one_return_disposition_costs
 USING(tenant_id::text=current_setting('one.tenant_id',true))
 WITH CHECK(tenant_id::text=current_setting('one.tenant_id',true));
CREATE TRIGGER one_return_disposition_cost_immutable BEFORE UPDATE OR DELETE
 ON one_return_disposition_costs FOR EACH ROW EXECUTE FUNCTION one_protect_audit();

CREATE FUNCTION one_resolve_and_value_customer_quarantine(
 p_tenant uuid,p_return uuid,p_disposition uuid,p_action text,
 p_qty numeric,p_target uuid,p_stock_out uuid,p_stock_in uuid,
 p_period uuid,p_inventory_acct text,p_disposal_expense text,
 p_journal uuid,p_actor uuid
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE
 r one_return_jobs%ROWTYPE;
 src one_stock_events%ROWTYPE;
 original one_customer_return_cost_links%ROWTYPE;
 origin_value one_stock_value_events%ROWTYPE;
 q one_stock_value_positions%ROWTYPE;
 t one_stock_value_positions%ROWTYPE;
 physical_q one_stock_balances%ROWTYPE;
 physical_t one_stock_balances%ROWTYPE;
 previous_qty numeric(24,3);
 previous_amount numeric(24,3);
 cost numeric(20,3);
 remainder_qty numeric(20,3);
 remainder_value numeric(20,3);
 scale integer;
 source_order uuid;
BEGIN
 IF p_tenant IS NULL OR p_return IS NULL OR p_disposition IS NULL
  OR p_action NOT IN ('restock','dispose') OR p_action IS NULL
  OR p_qty IS NULL OR p_qty<=0 OR p_qty<>round(p_qty,3)
  OR p_stock_out IS NULL OR p_period IS NULL OR
  p_inventory_acct IS NULL OR p_disposal_expense IS NULL
  OR p_actor IS NULL
  OR current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text
  OR current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text
  OR NOT EXISTS(SELECT 1 FROM one_memberships m
     WHERE m.tenant_id=p_tenant AND m.actor_id=p_actor AND m.active
       AND m.role='company_admin') THEN
   RAISE EXCEPTION 'Authorized costed QA disposition required';
 END IF;
 IF (p_action='dispose' AND (p_journal IS NULL OR
      p_target IS NOT NULL OR p_stock_in IS NOT NULL)) OR
    (p_action='restock' AND (p_journal IS NOT NULL OR
      p_target IS NULL OR p_stock_in IS NULL)) THEN
   RAISE EXCEPTION 'Invalid disposal journal or restock transfer parameters';
 END IF;
 SELECT e.order_id INTO source_order FROM one_return_jobs job
 JOIN one_stock_events e ON e.tenant_id=job.tenant_id AND
   e.id=job.source_event_id
 WHERE job.tenant_id=p_tenant AND job.id=p_return;
 IF source_order IS NULL THEN RAISE EXCEPTION 'Missing source order'; END IF;
 PERFORM 1 FROM one_commercial_orders
  WHERE tenant_id=p_tenant AND id=source_order FOR UPDATE;
 SELECT * INTO r FROM one_return_jobs
  WHERE tenant_id=p_tenant AND id=p_return FOR UPDATE;
 IF NOT FOUND OR r.status<>'completed' OR r.return_kind<>'customer'
  OR r.accepted_qty< p_qty THEN
  RAISE EXCEPTION 'Only costed accepted customer units qualify';
 END IF;
 SELECT * INTO original FROM one_customer_return_cost_links
  WHERE tenant_id=p_tenant AND return_job_id=p_return FOR UPDATE;
 IF NOT FOUND OR original.quantity<>r.accepted_qty THEN
  RAISE EXCEPTION 'Return has no complete historical source valuation';
 END IF;
 SELECT * INTO src FROM one_stock_events
  WHERE tenant_id=p_tenant AND id=r.source_event_id;
 SELECT * INTO origin_value FROM one_stock_value_events
  WHERE tenant_id=p_tenant AND stock_event_id=src.id;
 IF NOT FOUND OR origin_value.movement_kind<>'sale_dispatch' THEN
  RAISE EXCEPTION 'Source sale valuation unavailable';
 END IF;
 SELECT minor_units INTO scale FROM one_currency_precision
  WHERE code=origin_value.currency;
 IF scale IS NULL THEN RAISE EXCEPTION 'Unknown valuation currency'; END IF;
 SELECT coalesce(sum(quantity),0),coalesce(sum(amount),0)
 INTO previous_qty,previous_amount FROM one_return_disposition_costs
 WHERE tenant_id=p_tenant AND return_job_id=p_return;
 IF previous_qty+p_qty>original.quantity THEN
  RAISE EXCEPTION 'QA disposition would exceed historical accepted quantity';
 END IF;
 cost=CASE WHEN previous_qty+p_qty=original.quantity
  THEN original.amount-previous_amount
  ELSE round(original.amount*p_qty/original.quantity,scale) END;
 IF cost<=0 OR cost+previous_amount>original.amount THEN
  RAISE EXCEPTION 'Source historical inventory value exhausted';
 END IF;
 IF NOT EXISTS(SELECT 1 FROM one_accounts WHERE tenant_id=p_tenant
   AND id=p_inventory_acct AND kind='asset' AND active AND posting_allowed)
 OR NOT EXISTS(SELECT 1 FROM one_accounts WHERE tenant_id=p_tenant
   AND id=p_disposal_expense AND kind='expense' AND active AND posting_allowed) THEN
  RAISE EXCEPTION 'Approved inventory/disposal accounts missing';
 END IF;
 -- Physical event is written first; a cost mismatch/error undoes that
 -- same SQL transaction, not just its journal rows.
 PERFORM one_resolve_return_quarantine(p_tenant,p_return,p_disposition,
  p_action,p_qty,p_target,p_stock_out,p_stock_in,p_actor);
 SELECT * INTO physical_q FROM one_stock_balances
  WHERE tenant_id=p_tenant AND warehouse_id=r.destination_warehouse_id
    AND product_id=src.product_id FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'Missing quarantine physical bucket'; END IF;
 SELECT * INTO q FROM one_stock_value_positions
  WHERE tenant_id=p_tenant AND warehouse_id=r.destination_warehouse_id
    AND product_id=src.product_id FOR UPDATE;
 IF NOT FOUND OR q.currency<>origin_value.currency OR q.qty<p_qty
  OR q.book_value<cost THEN
  RAISE EXCEPTION 'Insufficient historically valued quarantine goods';
 END IF;
 remainder_qty=q.qty-p_qty;
 remainder_value=q.book_value-cost;
 IF remainder_qty<>physical_q.on_hand OR remainder_value<0
 OR (remainder_qty=0 AND remainder_value<>0)
 OR (remainder_qty>0 AND remainder_value<=0) THEN
  RAISE EXCEPTION 'QA cost and physical quarantine balances differ';
 END IF;
 PERFORM set_config('one.valuation_context','yes',true);
 UPDATE one_stock_value_positions SET qty=remainder_qty,book_value=remainder_value
  WHERE tenant_id=p_tenant AND warehouse_id=r.destination_warehouse_id
    AND product_id=src.product_id;

 IF p_action='restock' THEN
  SELECT * INTO physical_t FROM one_stock_balances
   WHERE tenant_id=p_tenant AND warehouse_id=p_target
     AND product_id=src.product_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'Missing restock target physical balance'; END IF;
  INSERT INTO one_stock_value_positions(
   tenant_id,warehouse_id,product_id,currency)
  VALUES(p_tenant,p_target,src.product_id,origin_value.currency)
  ON CONFLICT(tenant_id,warehouse_id,product_id) DO NOTHING;
  SELECT * INTO t FROM one_stock_value_positions
   WHERE tenant_id=p_tenant AND warehouse_id=p_target
     AND product_id=src.product_id FOR UPDATE;
  IF t.currency<>origin_value.currency OR
    t.qty+p_qty<>physical_t.on_hand THEN
   RAISE EXCEPTION 'Target stocked units/currency do not match value transfer';
  END IF;
  UPDATE one_stock_value_positions SET
    qty=qty+p_qty,book_value=book_value+cost
   WHERE tenant_id=p_tenant AND warehouse_id=p_target
     AND product_id=src.product_id;
 ELSE
  INSERT INTO one_journal_entries(
   tenant_id,id,period_id,reference,idempotency_key,occurred_on,currency)
  VALUES(p_tenant,p_journal,p_period,
    'return-disposal:'||p_disposition::text,
    'return-disposal:'||p_disposition::text,current_date,
    origin_value.currency);
  INSERT INTO one_journal_lines(
   tenant_id,entry_id,line_number,account_id,debit,credit)
  VALUES(p_tenant,p_journal,1,p_disposal_expense,cost,0),
        (p_tenant,p_journal,2,p_inventory_acct,0,cost);
  PERFORM one_post_journal(p_tenant,p_journal,p_actor);
 END IF;
 INSERT INTO one_return_disposition_costs(
  tenant_id,disposition_id,return_job_id,quantity,amount,
  action,journal_id,actor_id)
 VALUES(p_tenant,p_disposition,p_return,p_qty,cost,
  p_action,p_journal,p_actor);
 RETURN p_disposition;
END $$;
REVOKE ALL ON one_return_disposition_costs FROM PUBLIC;
REVOKE ALL ON FUNCTION one_resolve_and_value_customer_quarantine(
 uuid,uuid,uuid,text,numeric,uuid,uuid,uuid,uuid,text,text,uuid,uuid) FROM PUBLIC;
COMMIT;
