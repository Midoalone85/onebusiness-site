-- ONE ERP Next / 025 | IAS 2 lower-of-cost-and-NRV posted allowance (CI).
-- This is an isolated accounting-control proof, NOT a production IFRS assertion.
-- Original stock valuation remains GROSS COST; NRV allowance is a contra asset.
-- Active allowance blocks further goods movements until a reviewed
-- allocation/derecognition workflow is implemented.
BEGIN;

CREATE TABLE one_inventory_nrv_positions (
  tenant_id uuid NOT NULL,
  warehouse_id uuid NOT NULL,
  product_id uuid NOT NULL,
  currency text NOT NULL REFERENCES one_currency_precision(code),
  cost_qty numeric(20,3) NOT NULL CHECK(cost_qty>0),
  gross_cost numeric(20,3) NOT NULL CHECK(gross_cost>0),
  allowance numeric(20,3) NOT NULL DEFAULT 0 CHECK(allowance>=0),
  allowance_account_id text NOT NULL,
  expense_account_id text NOT NULL,
  revision integer NOT NULL DEFAULT 1 CHECK(revision>0),
  updated_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
  PRIMARY KEY(tenant_id,warehouse_id,product_id),
  FOREIGN KEY(tenant_id,warehouse_id,product_id)
    REFERENCES one_stock_value_positions(tenant_id,warehouse_id,product_id),
  FOREIGN KEY(tenant_id,allowance_account_id)
    REFERENCES one_accounts(tenant_id,id),
  FOREIGN KEY(tenant_id,expense_account_id)
    REFERENCES one_accounts(tenant_id,id),
  CHECK(allowance<=gross_cost)
);
ALTER TABLE one_inventory_nrv_positions ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_inventory_nrv_positions FORCE ROW LEVEL SECURITY;
CREATE POLICY one_nrv_position_scope ON one_inventory_nrv_positions
  USING(tenant_id::text=current_setting('one.tenant_id',true))
  WITH CHECK(tenant_id::text=current_setting('one.tenant_id',true));

CREATE FUNCTION one_nrv_position_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP='DELETE' OR
     current_setting('one.nrv_context',true) IS DISTINCT FROM 'yes' THEN
    RAISE EXCEPTION 'NRV position changes require controlled valuation operation';
  END IF;
  IF TG_OP='UPDATE' AND
    ROW(NEW.tenant_id,NEW.warehouse_id,NEW.product_id,NEW.currency,
        NEW.allowance_account_id,NEW.expense_account_id)
    IS DISTINCT FROM
    ROW(OLD.tenant_id,OLD.warehouse_id,OLD.product_id,OLD.currency,
        OLD.allowance_account_id,OLD.expense_account_id) THEN
    RAISE EXCEPTION 'NRV identity, currency and contra accounts cannot change';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER one_nrv_position_approved
 BEFORE INSERT OR UPDATE OR DELETE ON one_inventory_nrv_positions
 FOR EACH ROW EXECUTE FUNCTION one_nrv_position_guard();

CREATE TABLE one_inventory_nrv_events (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  warehouse_id uuid NOT NULL,
  product_id uuid NOT NULL,
  journal_id uuid NOT NULL,
  quantity numeric(20,3) NOT NULL CHECK(quantity>0),
  gross_cost numeric(20,3) NOT NULL CHECK(gross_cost>0),
  prior_allowance numeric(20,3) NOT NULL CHECK(prior_allowance>=0),
  new_allowance numeric(20,3) NOT NULL CHECK(new_allowance>=0),
  adjustment numeric(20,3) NOT NULL CHECK(adjustment<>0),
  net_value_after numeric(20,3) NOT NULL CHECK(net_value_after>=0),
  selling_price_per_unit numeric(20,3) NOT NULL CHECK(selling_price_per_unit>=0),
  completion_per_unit numeric(20,3) NOT NULL CHECK(completion_per_unit>=0),
  selling_cost_per_unit numeric(20,3) NOT NULL CHECK(selling_cost_per_unit>=0),
  evidence_reference text NOT NULL CHECK(length(btrim(evidence_reference)) BETWEEN 3 AND 200),
  assessed_on date NOT NULL,
  actor_id uuid NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
  PRIMARY KEY(tenant_id,id),
  UNIQUE(tenant_id,journal_id),
  UNIQUE(tenant_id,warehouse_id,product_id,evidence_reference),
  FOREIGN KEY(tenant_id,warehouse_id,product_id)
    REFERENCES one_inventory_nrv_positions(tenant_id,warehouse_id,product_id),
  FOREIGN KEY(tenant_id,journal_id)
    REFERENCES one_journal_entries(tenant_id,id),
  CHECK(new_allowance=prior_allowance+adjustment),
  CHECK(net_value_after=gross_cost-new_allowance)
);
CREATE INDEX one_nrv_events_item_history ON one_inventory_nrv_events
 (tenant_id,warehouse_id,product_id,recorded_at);
ALTER TABLE one_inventory_nrv_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_inventory_nrv_events FORCE ROW LEVEL SECURITY;
CREATE POLICY one_nrv_event_scope ON one_inventory_nrv_events
  USING(tenant_id::text=current_setting('one.tenant_id',true))
  WITH CHECK(tenant_id::text=current_setting('one.tenant_id',true));
CREATE TRIGGER one_nrv_events_immutable BEFORE UPDATE OR DELETE
 ON one_inventory_nrv_events FOR EACH ROW EXECUTE FUNCTION one_protect_audit();

-- Conservative release gate. Every physical movement creates a stock event.
-- A pending allowance must be allocated on stock exit before release.
CREATE FUNCTION one_nrv_movement_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS(SELECT 1 FROM one_inventory_nrv_positions p
    WHERE p.tenant_id=NEW.tenant_id AND p.warehouse_id=NEW.warehouse_id
      AND p.product_id=NEW.product_id AND p.allowance>0
    FOR UPDATE) THEN
    RAISE EXCEPTION 'IAS 2 allowance must be allocated before physical stock movement';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER one_nrv_movement_freeze BEFORE INSERT ON one_stock_events
 FOR EACH ROW EXECUTE FUNCTION one_nrv_movement_guard();

-- Outbox records the adjustment atomically with its posted journal.
ALTER TABLE one_operation_outbox DROP CONSTRAINT one_operation_outbox_event_type_check;
ALTER TABLE one_operation_outbox ADD CONSTRAINT one_operation_outbox_event_type_check
 CHECK (event_type IN (
    'stock.purchase_received','stock.sale_dispatched',
    'stock.customer_returned','stock.vendor_returned',
    'stock.return_restock_out','stock.return_restock_in','stock.return_disposal',
    'delivery.created','delivery.picked','delivery.packed',
    'delivery.dispatched','delivery.delivered',
    'receiving.inspection_started','receiving.inspection_completed',
    'returns.requested','returns.completed','returns.rejected',
    'returns.restocked','returns.disposed',
    'finance.invoice_issued','finance.journal_posted',
    'finance.customer_return_credited','finance.inventory_nrv_adjusted'
 ));

CREATE FUNCTION one_post_inventory_nrv_adjustment(
 p_tenant uuid,p_warehouse uuid,p_product uuid,
 p_event uuid,p_journal uuid,p_period uuid,
 p_allowance_account text,p_expense_account text,p_actor uuid,
 p_expected_revision integer,p_expected_qty numeric,p_expected_cost numeric,
 p_unit_sale numeric,p_unit_completion numeric,p_unit_sell numeric,
 p_evidence text,p_on date
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE
 sb one_stock_balances%ROWTYPE;
 val one_stock_value_positions%ROWTYPE;
 prev one_inventory_nrv_positions%ROWTYPE;
 amt_scale integer;
 new_nrv numeric(20,3);
 new_allowance numeric(20,3);
 old_allowance numeric(20,3);
 change_amount numeric(20,3);
 next_revision integer;
BEGIN
 IF p_tenant IS NULL OR p_warehouse IS NULL OR p_product IS NULL OR
    p_event IS NULL OR p_journal IS NULL OR p_event=p_journal OR
    p_period IS NULL OR p_actor IS NULL OR p_on IS NULL OR
    p_allowance_account IS NULL OR p_expense_account IS NULL OR
    p_allowance_account=p_expense_account OR
    p_evidence IS NULL OR length(btrim(p_evidence)) NOT BETWEEN 3 AND 200 OR
    p_expected_revision IS NULL OR p_expected_revision<0 OR
    p_expected_qty IS NULL OR p_expected_cost IS NULL OR
    p_unit_sale IS NULL OR p_unit_completion IS NULL OR p_unit_sell IS NULL OR
    p_unit_sale<0 OR p_unit_completion<0 OR p_unit_sell<0 OR
    p_unit_sale::text='NaN' OR p_unit_completion::text='NaN' OR
    p_unit_sell::text='NaN' OR
    p_expected_qty<=0 OR p_expected_cost<=0 OR
    current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
    current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
    NOT EXISTS(SELECT 1 FROM one_memberships m
      WHERE m.tenant_id=p_tenant AND m.actor_id=p_actor AND m.active
      AND m.role='company_admin') THEN
  RAISE EXCEPTION 'IAS 2 assessment requires verified authorized company source';
 END IF;
 SELECT * INTO sb FROM one_stock_balances
    WHERE tenant_id=p_tenant AND warehouse_id=p_warehouse AND product_id=p_product
    FOR UPDATE;
 SELECT * INTO val FROM one_stock_value_positions
    WHERE tenant_id=p_tenant AND warehouse_id=p_warehouse AND product_id=p_product
    FOR UPDATE;
 IF sb.tenant_id IS NULL OR val.tenant_id IS NULL OR
    sb.on_hand<=0 OR sb.on_hand<>val.qty OR
    val.qty<>p_expected_qty OR val.book_value<>p_expected_cost OR
    val.book_value<=0 THEN
  RAISE EXCEPTION 'Trusted current physical stock / cost snapshot required';
 END IF;
 IF EXISTS(SELECT 1 FROM one_stock_events e
   WHERE e.tenant_id=p_tenant AND e.warehouse_id=p_warehouse
     AND e.product_id=p_product
     AND NOT EXISTS(SELECT 1 FROM one_stock_value_events v
       WHERE v.tenant_id=e.tenant_id AND v.stock_event_id=e.id)
     AND NOT EXISTS(SELECT 1 FROM one_return_disposition_costs dc
       JOIN one_return_dispositions d
         ON d.tenant_id=dc.tenant_id AND d.id=dc.disposition_id
       WHERE dc.tenant_id=e.tenant_id
         AND (d.stock_event_out_id=e.id OR d.stock_event_in_id=e.id))
 ) THEN
  RAISE EXCEPTION 'Unvalued source movement blocks IAS 2 reporting';
 END IF;
 SELECT minor_units INTO amt_scale
   FROM one_currency_precision WHERE code=val.currency;
 IF amt_scale IS NULL OR
    p_unit_sale<>round(p_unit_sale,amt_scale) OR
    p_unit_completion<>round(p_unit_completion,amt_scale) OR
    p_unit_sell<>round(p_unit_sell,amt_scale) THEN
  RAISE EXCEPTION 'NRV estimate currency precision invalid';
 END IF;
 IF p_unit_sale < p_unit_completion+p_unit_sell THEN
  RAISE EXCEPTION 'Negative per-unit NRV needs separate approved treatment';
 END IF;
 IF NOT EXISTS(SELECT 1 FROM one_accounts
    WHERE tenant_id=p_tenant AND id=p_allowance_account AND kind='asset'
    AND active AND posting_allowed)
  OR NOT EXISTS(SELECT 1 FROM one_accounts
    WHERE tenant_id=p_tenant AND id=p_expense_account AND kind='expense'
    AND active AND posting_allowed) THEN
  RAISE EXCEPTION 'Valid contra-inventory asset and NRV expense accounts required';
 END IF;
 -- Lock existing NRV state only after stock/valuation locks, same order used
 -- by posting and physical movement entrypoints to avoid stale concurrency.
 SELECT * INTO prev FROM one_inventory_nrv_positions
    WHERE tenant_id=p_tenant AND warehouse_id=p_warehouse
      AND product_id=p_product FOR UPDATE;
 IF FOUND THEN
  IF prev.revision<>p_expected_revision THEN
   RAISE EXCEPTION 'Stale IAS 2 adjustment revision';
  END IF;
  IF prev.currency<>val.currency OR
     prev.allowance_account_id<>p_allowance_account OR
     prev.expense_account_id<>p_expense_account OR
     (prev.allowance>0 AND (prev.gross_cost<>val.book_value OR
         prev.cost_qty<>val.qty)) THEN
   RAISE EXCEPTION 'Stored allowance cost/currency/GL mapping is inconsistent';
  END IF;
  old_allowance=prev.allowance;
  next_revision=prev.revision+1;
 ELSE
  IF p_expected_revision<>0 THEN
   RAISE EXCEPTION 'First IAS 2 adjustment requires revision zero';
  END IF;
  old_allowance=0;
  next_revision=1;
 END IF;
 new_nrv=least(val.book_value,
  round(val.qty*(p_unit_sale-p_unit_completion-p_unit_sell),amt_scale));
 new_allowance=val.book_value-new_nrv;
 change_amount=new_allowance-old_allowance;
 IF change_amount=0 THEN
  RAISE EXCEPTION 'No inventory valuation adjustment required';
 END IF;
 PERFORM set_config('one.nrv_context','yes',true);
 INSERT INTO one_inventory_nrv_positions(
   tenant_id,warehouse_id,product_id,currency,cost_qty,gross_cost,
   allowance,allowance_account_id,expense_account_id,revision)
 VALUES(p_tenant,p_warehouse,p_product,val.currency,val.qty,val.book_value,
   new_allowance,p_allowance_account,p_expense_account,1)
 ON CONFLICT(tenant_id,warehouse_id,product_id) DO UPDATE SET
   cost_qty=EXCLUDED.cost_qty,gross_cost=EXCLUDED.gross_cost,
   allowance=EXCLUDED.allowance,revision=next_revision,
   updated_at=transaction_timestamp();
 PERFORM set_config('one.nrv_context','',true);

 INSERT INTO one_journal_entries(
   tenant_id,id,period_id,reference,idempotency_key,occurred_on,currency)
 VALUES(p_tenant,p_journal,p_period,'inventory-nrv:'||p_event::text,
   'inventory-nrv:'||p_event::text,p_on,val.currency);
 IF change_amount>0 THEN
  INSERT INTO one_journal_lines(
     tenant_id,entry_id,line_number,account_id,debit,credit)
  VALUES(p_tenant,p_journal,1,p_expense_account,change_amount,0),
        (p_tenant,p_journal,2,p_allowance_account,0,change_amount);
 ELSE
  INSERT INTO one_journal_lines(
    tenant_id,entry_id,line_number,account_id,debit,credit)
  VALUES(p_tenant,p_journal,1,p_allowance_account,-change_amount,0),
        (p_tenant,p_journal,2,p_expense_account,0,-change_amount);
 END IF;
 PERFORM one_post_journal(p_tenant,p_journal,p_actor);

 INSERT INTO one_inventory_nrv_events(
   tenant_id,id,warehouse_id,product_id,journal_id,quantity,
   gross_cost,prior_allowance,new_allowance,adjustment,net_value_after,
   selling_price_per_unit,completion_per_unit,selling_cost_per_unit,
   evidence_reference,assessed_on,actor_id)
 VALUES(p_tenant,p_event,p_warehouse,p_product,p_journal,val.qty,val.book_value,
   old_allowance,new_allowance,change_amount,new_nrv,p_unit_sale,
   p_unit_completion,p_unit_sell,btrim(p_evidence),p_on,p_actor);
 INSERT INTO one_operation_outbox(tenant_id,event_type,source_id)
 VALUES(p_tenant,'finance.inventory_nrv_adjusted',p_event);
 RETURN p_event;
END $$;

CREATE FUNCTION one_inventory_nrv_reconciliation(
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
  SELECT coalesce(sum(CASE WHEN l.account_id=n.allowance_account_id
                          THEN l.credit-l.debit ELSE 0 END),0)::numeric
    AS posted_amount
  FROM one_inventory_nrv_events e
  JOIN one_journal_entries j ON j.tenant_id=e.tenant_id
    AND j.id=e.journal_id AND j.status='posted'
  JOIN one_journal_lines l ON l.tenant_id=j.tenant_id
    AND l.entry_id=j.id
  WHERE e.tenant_id=sb.tenant_id
    AND e.warehouse_id=sb.warehouse_id
    AND e.product_id=sb.product_id
 ) j
 WHERE sb.tenant_id=p_tenant AND sb.warehouse_id=p_warehouse
    AND sb.product_id=p_product;
END $$;
REVOKE ALL ON one_inventory_nrv_positions,one_inventory_nrv_events FROM PUBLIC;
REVOKE ALL ON FUNCTION one_post_inventory_nrv_adjustment(
 uuid,uuid,uuid,uuid,uuid,uuid,text,text,uuid,
 integer,numeric,numeric,numeric,numeric,numeric,text,date) FROM PUBLIC;
REVOKE ALL ON FUNCTION one_inventory_nrv_reconciliation(
 uuid,uuid,uuid,uuid) FROM PUBLIC;
COMMIT;
