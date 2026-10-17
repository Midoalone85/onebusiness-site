-- ONE ERP Next 019 | Atomic accepted customer return + original COGS reversal.
-- Disposable PostgreSQL 14 integration only. Not tax/credit-note issuance.
BEGIN;
ALTER TABLE one_stock_value_events DROP CONSTRAINT one_stock_value_events_movement_kind_check;
ALTER TABLE one_stock_value_events ADD CONSTRAINT one_stock_value_events_movement_kind_check
 CHECK(movement_kind IN ('purchase_receipt','sale_dispatch','customer_return'));

CREATE TABLE one_customer_return_cost_links (
 tenant_id uuid NOT NULL,
 return_job_id uuid NOT NULL,
 source_dispatch_event_id uuid NOT NULL,
 return_stock_event_id uuid NOT NULL,
 quantity numeric(20,3) NOT NULL CHECK(quantity>0),
 amount numeric(20,3) NOT NULL CHECK(amount>0),
 actor_id uuid NOT NULL,
 created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 PRIMARY KEY(tenant_id,return_job_id),
 UNIQUE(tenant_id,return_stock_event_id),
 FOREIGN KEY(tenant_id,return_job_id)
  REFERENCES one_return_jobs(tenant_id,id),
 FOREIGN KEY(tenant_id,source_dispatch_event_id)
  REFERENCES one_stock_events(tenant_id,id),
 FOREIGN KEY(tenant_id,return_stock_event_id)
  REFERENCES one_stock_value_events(tenant_id,stock_event_id)
);
CREATE INDEX one_customer_return_cost_by_source
 ON one_customer_return_cost_links(tenant_id,source_dispatch_event_id);
ALTER TABLE one_customer_return_cost_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_customer_return_cost_links FORCE ROW LEVEL SECURITY;
CREATE POLICY one_customer_return_cost_scope ON one_customer_return_cost_links
 USING (tenant_id::text=current_setting('one.tenant_id',true))
 WITH CHECK (tenant_id::text=current_setting('one.tenant_id',true));
CREATE TRIGGER one_customer_return_cost_immutable BEFORE UPDATE OR DELETE
 ON one_customer_return_cost_links FOR EACH ROW EXECUTE FUNCTION one_protect_audit();

CREATE FUNCTION one_accept_and_value_customer_return(
 p_tenant uuid,p_return uuid,p_accepted numeric,p_stock_event uuid,
 p_journal uuid,p_period uuid,p_quarantine_inventory text,p_cogs text,p_actor uuid
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE
 source_order uuid;
 r one_return_jobs%ROWTYPE;
 src one_stock_events%ROWTYPE;
 origin one_stock_value_events%ROWTYPE;
 q one_stock_value_positions%ROWTYPE;
 physical one_stock_balances%ROWTYPE;
 scale integer;
 prior_qty numeric(24,3);
 prior_amount numeric(24,3);
 reverse_cost numeric(20,3);
 new_qty numeric(20,3);
 new_value numeric(20,3);
 source_currency text;
BEGIN
 IF p_tenant IS NULL OR p_return IS NULL OR p_accepted IS NULL
   OR p_accepted<=0 OR p_accepted<>round(p_accepted,3)
   OR p_stock_event IS NULL OR p_journal IS NULL OR p_period IS NULL
   OR p_stock_event=p_journal OR p_quarantine_inventory IS NULL
   OR p_cogs IS NULL OR p_quarantine_inventory=p_cogs OR p_actor IS NULL
   OR current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text
   OR current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text
   OR NOT EXISTS(SELECT 1 FROM one_memberships m
    WHERE m.tenant_id=p_tenant AND m.actor_id=p_actor AND m.active
      AND m.role='company_admin') THEN
  RAISE EXCEPTION 'Authorized accepted customer return with cost is required';
 END IF;

 SELECT e.order_id INTO source_order FROM one_return_jobs job
 JOIN one_stock_events e ON e.tenant_id=job.tenant_id
   AND e.id=job.source_event_id
 WHERE job.tenant_id=p_tenant AND job.id=p_return;
 IF source_order IS NULL THEN RAISE EXCEPTION 'Unknown customer return'; END IF;
 PERFORM 1 FROM one_commercial_orders
  WHERE tenant_id=p_tenant AND id=source_order FOR UPDATE;
 SELECT * INTO r FROM one_return_jobs
  WHERE tenant_id=p_tenant AND id=p_return FOR UPDATE;
 IF NOT FOUND OR r.return_kind<>'customer' OR r.status<>'requested'
    OR p_accepted>r.quantity THEN
  RAISE EXCEPTION 'Only pending customer return can be accepted with cost';
 END IF;
 SELECT * INTO src FROM one_stock_events
  WHERE tenant_id=p_tenant AND id=r.source_event_id FOR UPDATE;
 IF NOT FOUND OR src.movement_kind<>'sale_dispatch' THEN
  RAISE EXCEPTION 'Original customer dispatch required';
 END IF;
 SELECT * INTO origin FROM one_stock_value_events
  WHERE tenant_id=p_tenant AND stock_event_id=src.id FOR UPDATE;
 IF NOT FOUND OR origin.movement_kind<>'sale_dispatch'
    OR origin.qty<>src.quantity THEN
  RAISE EXCEPTION 'Original posted COGS movement must be valued first';
 END IF;
 source_currency=origin.currency;
 SELECT minor_units INTO scale FROM one_currency_precision
  WHERE code=source_currency;
 IF scale IS NULL THEN RAISE EXCEPTION 'Unknown source value precision'; END IF;
 IF NOT EXISTS (SELECT 1 FROM one_accounts a WHERE
    a.tenant_id=p_tenant AND a.id=p_quarantine_inventory
    AND a.kind='asset' AND a.active AND a.posting_allowed) OR
    NOT EXISTS (SELECT 1 FROM one_accounts a WHERE
    a.tenant_id=p_tenant AND a.id=p_cogs
    AND a.kind='expense' AND a.active AND a.posting_allowed) THEN
  RAISE EXCEPTION 'Active inventory asset and original COGS expense required';
 END IF;

 SELECT coalesce(sum(quantity),0),coalesce(sum(amount),0)
 INTO prior_qty,prior_amount
 FROM one_customer_return_cost_links
 WHERE tenant_id=p_tenant AND source_dispatch_event_id=src.id;
 IF prior_qty+p_accepted>origin.qty THEN
  RAISE EXCEPTION 'Original sold quantity already recovered';
 END IF;
 reverse_cost=CASE WHEN prior_qty+p_accepted=origin.qty
  THEN origin.amount-prior_amount
  ELSE round(origin.amount*p_accepted/origin.qty,scale) END;
 IF reverse_cost<=0 OR prior_amount+reverse_cost>origin.amount THEN
  RAISE EXCEPTION 'Invalid original cost restoration amount';
 END IF;

 -- Performs physical quarantine receipt. A valuation or GL failure later
 -- aborts this function call, rolling back this stock transaction entirely.
 PERFORM one_finish_return(p_tenant,p_return,p_accepted,p_stock_event,p_actor);
 SELECT * INTO physical FROM one_stock_balances
  WHERE tenant_id=p_tenant AND warehouse_id=r.destination_warehouse_id
   AND product_id=src.product_id FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'Quarantine stock balance missing'; END IF;
 IF EXISTS(SELECT 1 FROM one_stock_events e WHERE
      e.tenant_id=p_tenant AND e.warehouse_id=r.destination_warehouse_id
      AND e.product_id=src.product_id AND e.id<>p_stock_event
      AND NOT EXISTS(SELECT 1 FROM one_stock_value_events v WHERE
        v.tenant_id=e.tenant_id AND v.stock_event_id=e.id)) THEN
  RAISE EXCEPTION 'Other unvalued quarantine changes: cost source is ambiguous';
 END IF;
 PERFORM set_config('one.valuation_context','yes',true);
 INSERT INTO one_stock_value_positions(
  tenant_id,warehouse_id,product_id,currency)
 VALUES(p_tenant,r.destination_warehouse_id,src.product_id,source_currency)
 ON CONFLICT(tenant_id,warehouse_id,product_id) DO NOTHING;
 SELECT * INTO q FROM one_stock_value_positions
  WHERE tenant_id=p_tenant AND warehouse_id=r.destination_warehouse_id
    AND product_id=src.product_id FOR UPDATE;
 IF q.currency<>source_currency THEN
  RAISE EXCEPTION 'Cross-currency quarantine requires reviewed revaluation';
 END IF;
 new_qty=q.qty+p_accepted;
 new_value=q.book_value+reverse_cost;
 IF new_qty<>physical.on_hand OR new_value<=0 THEN
  RAISE EXCEPTION 'Quarantine physical and valuation quantities diverge';
 END IF;
 UPDATE one_stock_value_positions SET qty=new_qty,book_value=new_value
 WHERE tenant_id=p_tenant AND warehouse_id=r.destination_warehouse_id
   AND product_id=src.product_id;
 INSERT INTO one_journal_entries(
  tenant_id,id,period_id,reference,idempotency_key,occurred_on,currency)
 VALUES(p_tenant,p_journal,p_period,'return-cost:'||p_return::text,
   'return-cost:'||p_return::text,current_date,source_currency);
 INSERT INTO one_journal_lines(
  tenant_id,entry_id,line_number,account_id,debit,credit)
 VALUES(p_tenant,p_journal,1,p_quarantine_inventory,reverse_cost,0),
       (p_tenant,p_journal,2,p_cogs,0,reverse_cost);
 PERFORM one_post_journal(p_tenant,p_journal,p_actor);
 INSERT INTO one_stock_value_events(
  tenant_id,stock_event_id,journal_id,movement_kind,qty,amount,
  currency,position_qty_after,position_value_after,actor_id)
 VALUES(p_tenant,p_stock_event,p_journal,'customer_return',p_accepted,
  reverse_cost,source_currency,new_qty,new_value,p_actor);
 INSERT INTO one_customer_return_cost_links(
  tenant_id,return_job_id,source_dispatch_event_id,
  return_stock_event_id,quantity,amount,actor_id)
 VALUES(p_tenant,p_return,src.id,p_stock_event,p_accepted,reverse_cost,p_actor);
 RETURN p_return;
END $$;
REVOKE ALL ON one_customer_return_cost_links FROM PUBLIC;
REVOKE ALL ON FUNCTION one_accept_and_value_customer_return(
 uuid,uuid,numeric,uuid,uuid,uuid,text,text,uuid) FROM PUBLIC;
COMMIT;
