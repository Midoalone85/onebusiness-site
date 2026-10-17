-- ONE ERP Next / 027: atomic sale of previously written-down stock.
-- PG14 disposable CI ONLY. No approved production API or inventory tax policy.
-- Keep original cost history, release allocated NRV allowance against COGS.
BEGIN;

ALTER TABLE one_inventory_nrv_positions
  DROP CONSTRAINT one_inventory_nrv_positions_cost_qty_check;
ALTER TABLE one_inventory_nrv_positions
  DROP CONSTRAINT one_inventory_nrv_positions_gross_cost_check;
ALTER TABLE one_inventory_nrv_positions
  ADD CONSTRAINT one_inventory_nrv_zero_or_positive_state
 CHECK(
   (cost_qty=0 AND gross_cost=0 AND allowance=0)
   OR (cost_qty>0 AND gross_cost>0 AND allowance<=gross_cost)
 );

CREATE TABLE one_inventory_nrv_sale_releases (
 tenant_id uuid NOT NULL,
 stock_event_id uuid NOT NULL,
 gross_cogs_journal_id uuid NOT NULL,
 allowance_release_journal_id uuid NOT NULL,
 warehouse_id uuid NOT NULL,
 product_id uuid NOT NULL,
 quantity numeric(20,3) NOT NULL CHECK(quantity>0),
 gross_cogs numeric(20,3) NOT NULL CHECK(gross_cogs>0),
 released_allowance numeric(20,3) NOT NULL CHECK(released_allowance>0),
 net_cogs numeric(20,3) NOT NULL CHECK(net_cogs>=0),
 allowance_after numeric(20,3) NOT NULL CHECK(allowance_after>=0),
 actor_id uuid NOT NULL,
 recorded_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 PRIMARY KEY(tenant_id,stock_event_id),
 UNIQUE(tenant_id,gross_cogs_journal_id),
 UNIQUE(tenant_id,allowance_release_journal_id),
 FOREIGN KEY(tenant_id,stock_event_id)
    REFERENCES one_stock_value_events(tenant_id,stock_event_id),
 FOREIGN KEY(tenant_id,gross_cogs_journal_id)
    REFERENCES one_journal_entries(tenant_id,id),
 FOREIGN KEY(tenant_id,allowance_release_journal_id)
    REFERENCES one_journal_entries(tenant_id,id),
 FOREIGN KEY(tenant_id,warehouse_id,product_id)
    REFERENCES one_inventory_nrv_positions(tenant_id,warehouse_id,product_id),
 CHECK(net_cogs=gross_cogs-released_allowance)
);
ALTER TABLE one_inventory_nrv_sale_releases ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_inventory_nrv_sale_releases FORCE ROW LEVEL SECURITY;
CREATE POLICY one_nrv_sale_releases_scope ON one_inventory_nrv_sale_releases
 USING(tenant_id::text=current_setting('one.tenant_id',true))
 WITH CHECK(tenant_id::text=current_setting('one.tenant_id',true));
CREATE TRIGGER one_nrv_sale_release_append_only BEFORE UPDATE OR DELETE
 ON one_inventory_nrv_sale_releases FOR EACH ROW
 EXECUTE FUNCTION one_protect_audit();

CREATE OR REPLACE FUNCTION one_nrv_movement_guard()
 RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE a numeric(20,3);
BEGIN
 SELECT p.allowance INTO a
 FROM one_inventory_nrv_positions p
 WHERE p.tenant_id=NEW.tenant_id AND p.warehouse_id=NEW.warehouse_id
   AND p.product_id=NEW.product_id FOR UPDATE;
 IF a>0 THEN
  -- Only the source-linked, already authorized sale wrapper may open a
  -- single stock-event exception. Even then, a FAILED GL rolls back all.
  IF NEW.movement_kind<>'sale_dispatch'
     OR current_setting('one.nrv_dispatch_event',true)
           IS DISTINCT FROM NEW.id::text
     OR current_setting('one.tenant_id',true)
           IS DISTINCT FROM NEW.tenant_id::text
     OR current_setting('one.actor_id',true)
           IS DISTINCT FROM NEW.actor_id::text
     OR NOT EXISTS(SELECT 1 FROM one_memberships m
       WHERE m.tenant_id=NEW.tenant_id AND m.actor_id=NEW.actor_id
       AND m.active AND m.role='company_admin') THEN
   RAISE EXCEPTION 'IAS 2 allowance must be allocated with an atomic valued sale';
  END IF;
 END IF;
 RETURN NEW;
END $$;

CREATE FUNCTION one_ship_and_allocate_nrv(
 p_tenant uuid,p_order uuid,p_line integer,p_qty numeric,
 p_stock_event uuid,p_gross_journal uuid,p_release_journal uuid,
 p_period uuid,p_inventory_account text,p_cogs_account text,
 p_allowance_account text,p_actor uuid
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE
 ord one_commercial_orders%ROWTYPE;
 ol one_commercial_order_lines%ROWTYPE;
 v one_stock_value_positions%ROWTYPE;
 bal one_stock_balances%ROWTYPE;
 prior one_inventory_nrv_positions%ROWTYPE;
 gross_stock_cost numeric(20,3);
 write_down_release numeric(20,3);
 next_qty numeric(20,3);
 next_cost numeric(20,3);
 next_allowance numeric(20,3);
 minor integer;
 valued_event one_stock_value_events%ROWTYPE;
BEGIN
 IF p_tenant IS NULL OR p_order IS NULL OR p_line IS NULL OR
   p_line<=0 OR p_stock_event IS NULL OR p_gross_journal IS NULL
   OR p_release_journal IS NULL OR p_period IS NULL OR p_actor IS NULL
   OR p_qty IS NULL OR p_qty<=0 OR p_qty<>round(p_qty,3)
   OR p_qty::text='NaN' OR
   p_stock_event IN (p_gross_journal,p_release_journal)
   OR p_gross_journal=p_release_journal OR
   p_inventory_account IS NULL OR p_cogs_account IS NULL OR
   p_allowance_account IS NULL OR
   p_inventory_account IN (p_cogs_account,p_allowance_account)
   OR p_cogs_account=p_allowance_account OR
   current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
   current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
   NOT EXISTS(SELECT 1 FROM one_memberships m
     WHERE m.tenant_id=p_tenant AND m.actor_id=p_actor AND m.active
       AND m.role='company_admin') THEN
  RAISE EXCEPTION 'Authorized separate COGS and NRV sale journals required';
 END IF;
 SELECT * INTO ord FROM one_commercial_orders
  WHERE tenant_id=p_tenant AND id=p_order FOR UPDATE;
 IF NOT FOUND OR ord.kind<>'sale' OR ord.status<>'confirmed' THEN
  RAISE EXCEPTION 'Confirmed sale order required for IAS 2 stock release';
 END IF;
 SELECT * INTO ol FROM one_commercial_order_lines
  WHERE tenant_id=p_tenant AND order_id=p_order AND line_no=p_line FOR UPDATE;
 IF NOT FOUND OR ol.ordered_qty-ol.fulfilled_qty<p_qty THEN
  RAISE EXCEPTION 'Sale exceeds unfulfilled ordered quantity';
 END IF;
 SELECT * INTO bal FROM one_stock_balances
  WHERE tenant_id=p_tenant AND warehouse_id=ol.warehouse_id
    AND product_id=ol.product_id FOR UPDATE;
 SELECT * INTO v FROM one_stock_value_positions
  WHERE tenant_id=p_tenant AND warehouse_id=ol.warehouse_id
    AND product_id=ol.product_id FOR UPDATE;
 SELECT * INTO prior FROM one_inventory_nrv_positions
  WHERE tenant_id=p_tenant AND warehouse_id=ol.warehouse_id
    AND product_id=ol.product_id FOR UPDATE;
 IF bal.tenant_id IS NULL OR v.tenant_id IS NULL OR prior.tenant_id IS NULL OR
    prior.allowance<=0 OR prior.cost_qty<>v.qty OR
    prior.gross_cost<>v.book_value OR v.qty<>bal.on_hand OR
    v.qty<p_qty OR bal.on_hand-bal.reserved<p_qty OR
    v.currency<>ord.currency OR prior.currency<>v.currency THEN
  RAISE EXCEPTION 'Original reviewed IAS 2 allowance and unreserved valued stock required';
 END IF;
 IF NOT EXISTS(SELECT 1 FROM one_accounts WHERE tenant_id=p_tenant
       AND id=p_inventory_account AND kind='asset' AND active AND posting_allowed)
    OR NOT EXISTS(SELECT 1 FROM one_accounts WHERE tenant_id=p_tenant
       AND id=p_allowance_account AND kind='asset' AND active AND posting_allowed)
    OR NOT EXISTS(SELECT 1 FROM one_accounts WHERE tenant_id=p_tenant
       AND id=p_cogs_account AND kind='expense' AND active AND posting_allowed)
    OR p_allowance_account<>prior.allowance_account_id THEN
  RAISE EXCEPTION 'Specific permitted inventory/contra asset/COGS accounts required';
 END IF;
 SELECT minor_units INTO minor FROM one_currency_precision
  WHERE code=v.currency;
 IF minor IS NULL THEN RAISE EXCEPTION 'Unsupported stock valuation currency'; END IF;
 next_qty=v.qty-p_qty;
 gross_stock_cost=CASE WHEN next_qty=0 THEN v.book_value
   ELSE round(v.book_value*p_qty/v.qty,minor) END;
 write_down_release=CASE WHEN next_qty=0 THEN prior.allowance
   ELSE round(prior.allowance*p_qty/v.qty,minor) END;
 next_cost=v.book_value-gross_stock_cost;
 next_allowance=prior.allowance-write_down_release;
 IF gross_stock_cost<=0 OR write_down_release<=0
    OR write_down_release>gross_stock_cost
    OR next_allowance<0 OR next_allowance>next_cost
    OR (next_qty=0 AND (next_cost<>0 OR next_allowance<>0))
    OR (next_qty>0 AND next_cost<=0) THEN
  RAISE EXCEPTION 'NRV allocation lacks supported cost or amount precision';
 END IF;

 -- Existing physical cost and gross COGS functions must all commit or none.
 PERFORM set_config('one.nrv_dispatch_event',p_stock_event::text,true);
 PERFORM one_ship_and_value_goods(
   p_tenant,p_order,p_line,p_qty,p_stock_event,p_gross_journal,p_period,
   p_inventory_account,p_cogs_account,p_actor);
 PERFORM set_config('one.nrv_dispatch_event','',true);

 SELECT * INTO valued_event FROM one_stock_value_events
  WHERE tenant_id=p_tenant AND stock_event_id=p_stock_event;
 IF NOT FOUND OR valued_event.movement_kind<>'sale_dispatch' OR
    valued_event.journal_id<>p_gross_journal OR
    valued_event.qty<>p_qty OR valued_event.amount<>gross_stock_cost OR
    valued_event.position_qty_after<>next_qty OR
    valued_event.position_value_after<>next_cost THEN
  RAISE EXCEPTION 'Gross COGS stock-event valuation differs from original book';
 END IF;
 PERFORM set_config('one.nrv_context','yes',true);
 UPDATE one_inventory_nrv_positions SET
   cost_qty=next_qty,gross_cost=next_cost,allowance=next_allowance,
   revision=revision+1,updated_at=transaction_timestamp()
 WHERE tenant_id=p_tenant AND warehouse_id=ol.warehouse_id
   AND product_id=ol.product_id;
 PERFORM set_config('one.nrv_context','',true);

 INSERT INTO one_journal_entries(
   tenant_id,id,period_id,reference,idempotency_key,occurred_on,currency)
 VALUES(p_tenant,p_release_journal,p_period,
    'nrv-sale-release:'||p_stock_event::text,
    'nrv-sale-release:'||p_stock_event::text,current_date,v.currency);
 INSERT INTO one_journal_lines(
   tenant_id,entry_id,line_number,account_id,debit,credit)
 VALUES(p_tenant,p_release_journal,1,p_allowance_account,write_down_release,0),
       (p_tenant,p_release_journal,2,p_cogs_account,0,write_down_release);
 PERFORM one_post_journal(p_tenant,p_release_journal,p_actor);
 INSERT INTO one_inventory_nrv_sale_releases(
   tenant_id,stock_event_id,gross_cogs_journal_id,
   allowance_release_journal_id,warehouse_id,product_id,quantity,gross_cogs,
   released_allowance,net_cogs,allowance_after,actor_id)
 VALUES(p_tenant,p_stock_event,p_gross_journal,p_release_journal,
   ol.warehouse_id,ol.product_id,p_qty,gross_stock_cost,
   write_down_release,gross_stock_cost-write_down_release,next_allowance,p_actor);
 RETURN p_stock_event;
END $$;

ALTER TABLE one_operation_outbox DROP CONSTRAINT one_operation_outbox_event_type_check;
ALTER TABLE one_operation_outbox ADD CONSTRAINT one_operation_outbox_event_type_check
 CHECK(event_type IN (
    'stock.purchase_received','stock.sale_dispatched',
    'stock.customer_returned','stock.vendor_returned',
    'stock.return_restock_out','stock.return_restock_in','stock.return_disposal',
    'delivery.created','delivery.picked','delivery.packed',
    'delivery.dispatched','delivery.delivered',
    'receiving.inspection_started','receiving.inspection_completed',
    'returns.requested','returns.completed','returns.rejected',
    'returns.restocked','returns.disposed',
    'finance.invoice_issued','finance.journal_posted',
    'finance.customer_return_credited','finance.inventory_nrv_adjusted',
    'finance.inventory_nrv_released'
 ));
CREATE FUNCTION one_nrv_sale_release_outbox() RETURNS trigger
 LANGUAGE plpgsql SECURITY INVOKER AS $$
BEGIN
 INSERT INTO one_operation_outbox(tenant_id,event_type,source_id)
 VALUES(NEW.tenant_id,'finance.inventory_nrv_released',NEW.stock_event_id);
 RETURN NEW;
END $$;
CREATE TRIGGER one_nrv_release_outbox AFTER INSERT ON one_inventory_nrv_sale_releases
 FOR EACH ROW EXECUTE FUNCTION one_nrv_sale_release_outbox();

-- Revised read model: previously booked write-down less all posted
-- reclassifications to COGS; do not pretend allowance never got released.
CREATE OR REPLACE FUNCTION one_inventory_nrv_reconciliation(
 p_tenant uuid,p_warehouse uuid,p_product uuid,p_actor uuid
) RETURNS TABLE(
 physical_qty numeric, valued_qty numeric, gross_cost numeric,
 allowance numeric, net_carrying_amount numeric, posted_allowance numeric,
 reconciliation_ok boolean
) LANGUAGE plpgsql STABLE SECURITY INVOKER AS $$
BEGIN
 IF p_tenant IS NULL OR p_warehouse IS NULL OR p_product IS NULL OR
    p_actor IS NULL OR
    current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
    current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
    NOT EXISTS(SELECT 1 FROM one_memberships m WHERE m.tenant_id=p_tenant
     AND m.actor_id=p_actor AND m.active
     AND m.role IN ('company_admin','accountant','viewer')) THEN
  RAISE EXCEPTION 'IAS 2 finance report requires a company member';
 END IF;
 RETURN QUERY
 SELECT sb.on_hand::numeric,v.qty::numeric,v.book_value::numeric,
   coalesce(n.allowance,0)::numeric,
   (v.book_value-coalesce(n.allowance,0))::numeric,
   coalesce(j.posted_amount,0)::numeric,
   (sb.on_hand=v.qty
     AND coalesce(n.allowance,0)<=v.book_value
     AND coalesce(j.posted_amount,0)=coalesce(n.allowance,0)
     AND (coalesce(n.allowance,0)=0 OR
       (n.cost_qty=v.qty AND n.gross_cost=v.book_value)))::boolean
 FROM one_stock_balances sb
 JOIN one_stock_value_positions v ON v.tenant_id=sb.tenant_id
   AND v.warehouse_id=sb.warehouse_id AND v.product_id=sb.product_id
 LEFT JOIN one_inventory_nrv_positions n
   ON n.tenant_id=sb.tenant_id AND n.warehouse_id=sb.warehouse_id
     AND n.product_id=sb.product_id
 CROSS JOIN LATERAL(
  SELECT (
    coalesce((
     SELECT sum(l.credit-l.debit)
      FROM one_inventory_nrv_events e
      JOIN one_journal_entries j ON j.tenant_id=e.tenant_id
       AND j.id=e.journal_id AND j.status='posted'
      JOIN one_journal_lines l ON l.tenant_id=j.tenant_id
       AND l.entry_id=j.id AND l.account_id=n.allowance_account_id
      WHERE e.tenant_id=sb.tenant_id
       AND e.warehouse_id=sb.warehouse_id AND e.product_id=sb.product_id
    ),0)
    +coalesce((
     SELECT sum(l.credit-l.debit)
      FROM one_inventory_nrv_sale_releases r
      JOIN one_journal_entries j ON j.tenant_id=r.tenant_id
       AND j.id=r.allowance_release_journal_id AND j.status='posted'
      JOIN one_journal_lines l ON l.tenant_id=j.tenant_id
       AND l.entry_id=j.id AND l.account_id=n.allowance_account_id
      WHERE r.tenant_id=sb.tenant_id
       AND r.warehouse_id=sb.warehouse_id AND r.product_id=sb.product_id
    ),0)
   )::numeric AS posted_amount
 ) j
 WHERE sb.tenant_id=p_tenant AND sb.warehouse_id=p_warehouse
    AND sb.product_id=p_product;
END $$;

REVOKE ALL ON one_inventory_nrv_sale_releases FROM PUBLIC;
REVOKE ALL ON FUNCTION one_ship_and_allocate_nrv(
 uuid,uuid,integer,numeric,uuid,uuid,uuid,uuid,text,text,text,uuid
) FROM PUBLIC;
REVOKE ALL ON FUNCTION one_nrv_sale_release_outbox() FROM PUBLIC;
COMMIT;
