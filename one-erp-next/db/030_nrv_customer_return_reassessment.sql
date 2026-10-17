-- ONE ERP Next / 030 | source-aware NRV customer return into clean QA quarantine.
-- PG14 CI only: a strictly reviewed *impaired* return. No automatic refund,
-- electronic credit, VAT, warehouse release or approval signature service.
BEGIN;

-- Exceptional completion must be source-authenticated and fully accounted.
-- Ordinary completion of an NRV-affected sale remains blocked by 029.
CREATE OR REPLACE FUNCTION one_nrv_adjusted_customer_return_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
BEGIN
 IF TG_OP='UPDATE' AND OLD.status='requested'
    AND NEW.status='completed' AND NEW.return_kind='customer'
    AND EXISTS(SELECT 1 FROM one_inventory_nrv_sale_releases s
      WHERE s.tenant_id=NEW.tenant_id AND s.stock_event_id=NEW.source_event_id)
 THEN
   IF current_setting('one.nrv_customer_return_job',true)
         IS DISTINCT FROM NEW.id::text OR
      current_setting('one.nrv_customer_return_stock',true)
         IS DISTINCT FROM NEW.processed_event_id::text OR
      current_setting('one.actor_id',true) IS DISTINCT FROM NEW.reviewed_by::text OR
      current_setting('one.tenant_id',true) IS DISTINCT FROM NEW.tenant_id::text OR
      NEW.accepted_qty IS NULL OR NEW.accepted_qty<=0 OR
      NOT EXISTS(SELECT 1 FROM one_memberships m
       WHERE m.tenant_id=NEW.tenant_id AND m.actor_id=NEW.reviewed_by
         AND m.active AND m.role='company_admin') THEN
     RAISE EXCEPTION 'NRV-affected customer return needs accounted QA review';
   END IF;
 END IF;
 RETURN NEW;
END $$;

CREATE TABLE one_nrv_customer_return_reviews (
 tenant_id uuid NOT NULL,
 return_job_id uuid NOT NULL,
 original_sold_event_id uuid NOT NULL,
 physical_return_event_id uuid NOT NULL,
 gross_return_journal_id uuid NOT NULL,
 new_nrv_event_id uuid NOT NULL,
 new_nrv_journal_id uuid NOT NULL,
 quantity numeric(20,3) NOT NULL CHECK(quantity>0),
 original_gross_return_cost numeric(20,3) NOT NULL CHECK(original_gross_return_cost>0),
 reassessed_nrv numeric(20,3) NOT NULL
    CHECK(reassessed_nrv>=0 AND reassessed_nrv<original_gross_return_cost),
 new_allowance numeric(20,3) NOT NULL CHECK(new_allowance>0),
 quarantine_warehouse_id uuid NOT NULL,
 evidence_reference text NOT NULL CHECK(length(btrim(evidence_reference)) BETWEEN 3 AND 200),
 actor_id uuid NOT NULL,
 reviewed_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 PRIMARY KEY(tenant_id,return_job_id),
 UNIQUE(tenant_id,physical_return_event_id),
 UNIQUE(tenant_id,new_nrv_event_id),
 UNIQUE(tenant_id,new_nrv_journal_id),
 FOREIGN KEY(tenant_id,return_job_id)
   REFERENCES one_return_jobs(tenant_id,id),
 FOREIGN KEY(tenant_id,original_sold_event_id)
   REFERENCES one_stock_events(tenant_id,id),
 FOREIGN KEY(tenant_id,physical_return_event_id)
   REFERENCES one_stock_value_events(tenant_id,stock_event_id),
 FOREIGN KEY(tenant_id,gross_return_journal_id)
   REFERENCES one_journal_entries(tenant_id,id),
 FOREIGN KEY(tenant_id,new_nrv_event_id)
   REFERENCES one_inventory_nrv_events(tenant_id,id),
 FOREIGN KEY(tenant_id,new_nrv_journal_id)
   REFERENCES one_journal_entries(tenant_id,id),
 FOREIGN KEY(tenant_id,quarantine_warehouse_id)
   REFERENCES one_warehouses(tenant_id,id),
 CHECK(new_allowance=original_gross_return_cost-reassessed_nrv)
);
ALTER TABLE one_nrv_customer_return_reviews ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_nrv_customer_return_reviews FORCE ROW LEVEL SECURITY;
CREATE POLICY one_nrv_return_review_scope ON one_nrv_customer_return_reviews
  USING(tenant_id::text=current_setting('one.tenant_id',true))
  WITH CHECK(tenant_id::text=current_setting('one.tenant_id',true));
CREATE TRIGGER one_nrv_customer_return_reviews_immutable BEFORE UPDATE OR DELETE
 ON one_nrv_customer_return_reviews FOR EACH ROW
 EXECUTE FUNCTION one_protect_audit();

ALTER TABLE one_operation_outbox
 DROP CONSTRAINT one_operation_outbox_event_type_check;
ALTER TABLE one_operation_outbox
 ADD CONSTRAINT one_operation_outbox_event_type_check CHECK(event_type IN(
   'stock.purchase_received','stock.sale_dispatched','stock.customer_returned',
   'stock.vendor_returned','stock.return_restock_out','stock.return_restock_in',
   'stock.return_disposal','delivery.created','delivery.picked','delivery.packed',
   'delivery.dispatched','delivery.delivered',
   'receiving.inspection_started','receiving.inspection_completed',
   'returns.requested','returns.completed','returns.rejected',
   'returns.restocked','returns.disposed','returns.nrv_reassessed',
   'finance.invoice_issued','finance.journal_posted',
   'finance.customer_return_credited','finance.inventory_nrv_adjusted',
   'finance.inventory_nrv_released'
 ));

CREATE FUNCTION one_accept_reassessed_nrv_customer_return(
 p_tenant uuid,p_return uuid,p_qty numeric,p_event uuid,p_gross_journal uuid,
 p_nrv_event uuid,p_nrv_journal uuid,p_period uuid,
 p_inventory text,p_cogs text,p_allowance text,p_nrv_loss text,
 p_actor uuid,p_unit_sale numeric,p_unit_completion numeric,
 p_unit_selling numeric,p_evidence text,p_assessed_on date
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE
 order_id uuid;
 r one_return_jobs%ROWTYPE;
 src one_stock_events%ROWTYPE;
 sold one_inventory_nrv_sale_releases%ROWTYPE;
 credit one_customer_return_cost_links%ROWTYPE;
 minor integer;
 nrv_amount numeric(20,3);
BEGIN
 IF p_tenant IS NULL OR p_return IS NULL OR p_actor IS NULL OR
   p_event IS NULL OR p_gross_journal IS NULL OR p_nrv_event IS NULL OR
   p_nrv_journal IS NULL OR p_period IS NULL OR p_assessed_on IS NULL OR
   p_qty IS NULL OR p_qty<=0 OR p_qty<>round(p_qty,3) OR
   p_qty::text='NaN' OR
   p_unit_sale IS NULL OR p_unit_completion IS NULL OR p_unit_selling IS NULL OR
   p_unit_sale<0 OR p_unit_completion<0 OR p_unit_selling<0 OR
   p_unit_sale::text='NaN' OR p_unit_completion::text='NaN' OR
   p_unit_selling::text='NaN' OR
   p_unit_sale<p_unit_completion+p_unit_selling OR
   p_evidence IS NULL OR length(btrim(p_evidence)) NOT BETWEEN 3 AND 200 OR
   p_assessed_on<>current_date OR
   p_inventory IS NULL OR p_cogs IS NULL OR
   p_allowance IS NULL OR p_nrv_loss IS NULL OR
   p_inventory IN (p_cogs,p_allowance,p_nrv_loss) OR
   p_cogs IN (p_allowance,p_nrv_loss) OR p_allowance=p_nrv_loss OR
   p_event IN (p_gross_journal,p_nrv_event,p_nrv_journal) OR
   p_gross_journal IN (p_nrv_event,p_nrv_journal) OR
   p_nrv_event=p_nrv_journal OR
   current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
   current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
   NOT EXISTS(SELECT 1 FROM one_memberships
    WHERE tenant_id=p_tenant AND actor_id=p_actor
      AND role='company_admin' AND active) THEN
    RAISE EXCEPTION 'Authenticated original-cost and fresh-NRV inspection required';
 END IF;
 SELECT s.order_id INTO order_id FROM one_return_jobs j
 JOIN one_stock_events s ON s.tenant_id=j.tenant_id AND s.id=j.source_event_id
 WHERE j.tenant_id=p_tenant AND j.id=p_return;
 IF order_id IS NULL THEN RAISE EXCEPTION 'Original sale source not found'; END IF;
 PERFORM 1 FROM one_commercial_orders WHERE tenant_id=p_tenant
    AND id=order_id FOR UPDATE;
 SELECT * INTO r FROM one_return_jobs WHERE tenant_id=p_tenant
    AND id=p_return FOR UPDATE;
 IF NOT FOUND OR r.status<>'requested' OR r.return_kind<>'customer' OR
    p_qty>r.quantity OR r.destination_warehouse_id IS NULL THEN
    RAISE EXCEPTION 'Only open customer RMA into QA quarantine eligible';
 END IF;
 SELECT * INTO src FROM one_stock_events WHERE tenant_id=p_tenant
    AND id=r.source_event_id FOR UPDATE;
 SELECT * INTO sold FROM one_inventory_nrv_sale_releases
    WHERE tenant_id=p_tenant AND stock_event_id=src.id;
 IF NOT FOUND OR src.movement_kind<>'sale_dispatch' OR sold.quantity<>src.quantity
    OR sold.released_allowance<=0 THEN
    RAISE EXCEPTION 'Original sale must have posted an allocated NRV release';
 END IF;
 IF NOT EXISTS(SELECT 1 FROM one_warehouses WHERE tenant_id=p_tenant
       AND id=r.destination_warehouse_id AND warehouse_type='quarantine'
       AND active) THEN
    RAISE EXCEPTION 'Active QA warehouse required';
 END IF;
 -- Until receipt-specific NRV cost layers exist, this RMA requires a clean
 -- single-source product bucket; never cost-mix an unrelated quarantine item.
 IF EXISTS(SELECT 1 FROM one_stock_balances b
     WHERE b.tenant_id=p_tenant AND b.warehouse_id=r.destination_warehouse_id
       AND b.product_id=src.product_id AND (b.on_hand<>0 OR b.reserved<>0))
    OR EXISTS(SELECT 1 FROM one_inventory_nrv_positions n
       WHERE n.tenant_id=p_tenant AND n.warehouse_id=r.destination_warehouse_id
         AND n.product_id=src.product_id)
    OR EXISTS(SELECT 1 FROM one_stock_events e
       WHERE e.tenant_id=p_tenant AND e.warehouse_id=r.destination_warehouse_id
         AND e.product_id=src.product_id) THEN
    RAISE EXCEPTION 'Mixed quarantine receipt requires specific NRV cost layers';
 END IF;
 PERFORM set_config('one.nrv_customer_return_job',p_return::text,true);
 PERFORM set_config('one.nrv_customer_return_stock',p_event::text,true);
 PERFORM one_accept_and_value_customer_return(
  p_tenant,p_return,p_qty,p_event,p_gross_journal,p_period,p_inventory,p_cogs,p_actor);
 PERFORM set_config('one.nrv_customer_return_job','',true);
 PERFORM set_config('one.nrv_customer_return_stock','',true);
 SELECT * INTO credit FROM one_customer_return_cost_links
   WHERE tenant_id=p_tenant AND return_job_id=p_return;
 IF NOT FOUND OR credit.quantity<>p_qty OR
    credit.return_stock_event_id<>p_event OR
    credit.source_dispatch_event_id<>src.id THEN
    RAISE EXCEPTION 'Original sale COGS was not recovered into QA';
 END IF;
 SELECT minor_units INTO minor
    FROM one_currency_precision WHERE code=(
      SELECT currency FROM one_stock_value_events
      WHERE tenant_id=p_tenant AND stock_event_id=p_event);
 IF minor IS NULL THEN RAISE EXCEPTION 'Unknown original sale currency'; END IF;
 IF p_unit_sale<>round(p_unit_sale,minor) OR
    p_unit_completion<>round(p_unit_completion,minor) OR
    p_unit_selling<>round(p_unit_selling,minor) THEN
    RAISE EXCEPTION 'NRV review precision invalid';
 END IF;
 nrv_amount=least(credit.amount,
    round(p_qty*(p_unit_sale-p_unit_completion-p_unit_selling),minor));
 IF nrv_amount>=credit.amount OR nrv_amount<0 THEN
    RAISE EXCEPTION 'This reviewed-return pilot requires a new write-down';
 END IF;
 PERFORM one_post_inventory_nrv_adjustment(
    p_tenant,r.destination_warehouse_id,src.product_id,
    p_nrv_event,p_nrv_journal,p_period,p_allowance,p_nrv_loss,p_actor,
    0,p_qty,credit.amount,p_unit_sale,p_unit_completion,p_unit_selling,
    p_evidence,p_assessed_on);
 INSERT INTO one_nrv_customer_return_reviews(
   tenant_id,return_job_id,original_sold_event_id,physical_return_event_id,
   gross_return_journal_id,new_nrv_event_id,new_nrv_journal_id,quantity,
   original_gross_return_cost,reassessed_nrv,new_allowance,
   quarantine_warehouse_id,evidence_reference,actor_id)
 VALUES(p_tenant,p_return,src.id,p_event,p_gross_journal,p_nrv_event,
   p_nrv_journal,p_qty,credit.amount,nrv_amount,credit.amount-nrv_amount,
   r.destination_warehouse_id,btrim(p_evidence),p_actor);
 INSERT INTO one_operation_outbox(tenant_id,event_type,source_id)
 VALUES(p_tenant,'returns.nrv_reassessed',p_return);
 RETURN p_return;
END $$;
REVOKE ALL ON one_nrv_customer_return_reviews FROM PUBLIC;
REVOKE ALL ON FUNCTION one_accept_reassessed_nrv_customer_return(
 uuid,uuid,numeric,uuid,uuid,uuid,uuid,uuid,text,text,text,text,
 uuid,numeric,numeric,numeric,text,date) FROM PUBLIC;
COMMIT;
