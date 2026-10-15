-- ONE ERP Next / 012: one operational return case, with explicit QA disposition.
-- PostgreSQL 14 development CI only. No production access or financial valuation.
BEGIN;
ALTER TABLE one_stock_events DROP CONSTRAINT one_stock_events_movement_kind_check;
ALTER TABLE one_stock_events ADD CONSTRAINT one_stock_events_movement_kind_check
  CHECK (movement_kind IN (
    'purchase_receipt','sale_dispatch','customer_return','vendor_return',
    'return_restock_out','return_restock_in','return_disposal'
  ));

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
    'finance.customer_return_credited'
  ));

CREATE TABLE one_return_dispositions (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  return_job_id uuid NOT NULL,
  action text NOT NULL CHECK(action IN ('restock','dispose')),
  quantity numeric(20,3) NOT NULL CHECK(quantity>0),
  from_warehouse_id uuid NOT NULL,
  to_warehouse_id uuid,
  stock_event_out_id uuid NOT NULL,
  stock_event_in_id uuid,
  actor_id uuid NOT NULL,
  happened_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
  PRIMARY KEY(tenant_id,id),
  UNIQUE(tenant_id,stock_event_out_id),
  UNIQUE(tenant_id,stock_event_in_id),
  FOREIGN KEY(tenant_id,return_job_id)
    REFERENCES one_return_jobs(tenant_id,id),
  FOREIGN KEY(tenant_id,stock_event_out_id)
    REFERENCES one_stock_events(tenant_id,id),
  FOREIGN KEY(tenant_id,stock_event_in_id)
    REFERENCES one_stock_events(tenant_id,id),
  FOREIGN KEY(tenant_id,from_warehouse_id)
    REFERENCES one_warehouses(tenant_id,id),
  FOREIGN KEY(tenant_id,to_warehouse_id)
    REFERENCES one_warehouses(tenant_id,id),
  CHECK ((action='restock' AND to_warehouse_id IS NOT NULL
     AND stock_event_in_id IS NOT NULL
     AND stock_event_in_id<>stock_event_out_id
     AND from_warehouse_id<>to_warehouse_id)
     OR (action='dispose' AND to_warehouse_id IS NULL
       AND stock_event_in_id IS NULL))
);
CREATE INDEX one_return_dispositions_by_case
  ON one_return_dispositions(tenant_id,return_job_id);
ALTER TABLE one_return_dispositions ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_return_dispositions FORCE ROW LEVEL SECURITY;
CREATE POLICY one_return_disposition_scope ON one_return_dispositions
  USING (tenant_id::text=current_setting('one.tenant_id',true))
  WITH CHECK (tenant_id::text=current_setting('one.tenant_id',true));
CREATE TRIGGER one_return_disposition_immutable BEFORE UPDATE OR DELETE
  ON one_return_dispositions FOR EACH ROW EXECUTE FUNCTION one_protect_audit();

CREATE FUNCTION one_resolve_return_quarantine(
  p_tenant uuid, p_return uuid, p_disposition uuid, p_action text,
  p_qty numeric(20,3), p_target uuid, p_event_out uuid,
  p_event_in uuid, p_actor uuid
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE
  r one_return_jobs%ROWTYPE;
  s one_stock_events%ROWTYPE;
  source_order uuid;
  processed_qty numeric(24,3);
BEGIN
  IF p_tenant IS NULL OR p_return IS NULL OR p_disposition IS NULL
    OR p_actor IS NULL OR p_event_out IS NULL
    OR p_qty IS NULL OR p_qty<=0 OR p_action NOT IN ('restock','dispose')
    OR p_action IS NULL
    OR current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text
    OR current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text
    OR NOT EXISTS (SELECT 1 FROM one_memberships WHERE tenant_id=p_tenant
      AND actor_id=p_actor AND active AND role='company_admin') THEN
    RAISE EXCEPTION 'Authenticated warehouse disposition required';
  END IF;
  IF (p_action='restock' AND
      (p_target IS NULL OR p_event_in IS NULL OR p_event_in=p_event_out))
     OR (p_action='dispose' AND
         (p_target IS NOT NULL OR p_event_in IS NOT NULL)) THEN
    RAISE EXCEPTION 'Invalid restock or disposal destinations and events';
  END IF;

  SELECT s.order_id INTO source_order
    FROM one_return_jobs r JOIN one_stock_events s
      ON s.tenant_id=r.tenant_id AND s.id=r.source_event_id
    WHERE r.tenant_id=p_tenant AND r.id=p_return;
  IF source_order IS NULL THEN RAISE EXCEPTION 'Unknown return source'; END IF;
  PERFORM 1 FROM one_commercial_orders
    WHERE tenant_id=p_tenant AND id=source_order FOR UPDATE;
  SELECT * INTO r FROM one_return_jobs
    WHERE tenant_id=p_tenant AND id=p_return FOR UPDATE;
  IF NOT FOUND OR r.return_kind<>'customer' OR r.status<>'completed'
     OR r.accepted_qty<=0 THEN
    RAISE EXCEPTION 'Only accepted customer return is eligible for QA disposition';
  END IF;
  SELECT * INTO s FROM one_stock_events
    WHERE tenant_id=p_tenant AND id=r.source_event_id;
  IF NOT FOUND OR s.movement_kind<>'sale_dispatch'
     OR NOT EXISTS (SELECT 1 FROM one_warehouses
         WHERE tenant_id=p_tenant AND id=r.destination_warehouse_id
           AND warehouse_type='quarantine' AND active) THEN
    RAISE EXCEPTION 'Quarantine source unavailable';
  END IF;
  IF p_action='restock' AND NOT EXISTS (
      SELECT 1 FROM one_warehouses
      WHERE tenant_id=p_tenant AND id=p_target
        AND warehouse_type='sellable' AND active
  ) THEN
    RAISE EXCEPTION 'Target must be a separately active sellable warehouse';
  END IF;

  SELECT coalesce(sum(quantity),0) INTO processed_qty
    FROM one_return_dispositions
    WHERE tenant_id=p_tenant AND return_job_id=p_return;
  IF processed_qty+p_qty>r.accepted_qty THEN
    RAISE EXCEPTION 'QA disposition exceeds accepted return units';
  END IF;

  -- The quarantine quantity must actually exist and be unreserved.
  PERFORM set_config('one.commerce_context','yes',true);
  UPDATE one_stock_balances
    SET on_hand=on_hand-p_qty
    WHERE tenant_id=p_tenant AND warehouse_id=r.destination_warehouse_id
      AND product_id=s.product_id AND on_hand-reserved>=p_qty;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'Quarantined stock is absent, reserved, or insufficient';
  END IF;
  IF p_action='restock' THEN
    INSERT INTO one_stock_balances(tenant_id,warehouse_id,product_id)
      VALUES(p_tenant,p_target,s.product_id)
      ON CONFLICT (tenant_id,warehouse_id,product_id) DO NOTHING;
    UPDATE one_stock_balances SET on_hand=on_hand+p_qty
      WHERE tenant_id=p_tenant AND warehouse_id=p_target AND product_id=s.product_id;
  END IF;
  INSERT INTO one_stock_events
    (tenant_id,id,order_id,line_no,warehouse_id,product_id,
     movement_kind,quantity,actor_id)
  VALUES(p_tenant,p_event_out,s.order_id,s.line_no,r.destination_warehouse_id,
         s.product_id,
         CASE WHEN p_action='restock' THEN 'return_restock_out'
           ELSE 'return_disposal' END,
         p_qty,p_actor);
  IF p_action='restock' THEN
    INSERT INTO one_stock_events
      (tenant_id,id,order_id,line_no,warehouse_id,product_id,
       movement_kind,quantity,actor_id)
    VALUES(p_tenant,p_event_in,s.order_id,s.line_no,p_target,s.product_id,
           'return_restock_in',p_qty,p_actor);
  END IF;
  INSERT INTO one_return_dispositions(
      tenant_id,id,return_job_id,action,quantity,from_warehouse_id,
      to_warehouse_id,stock_event_out_id,stock_event_in_id,actor_id
  ) VALUES(p_tenant,p_disposition,p_return,p_action,p_qty,
           r.destination_warehouse_id,p_target,p_event_out,p_event_in,p_actor);
  RETURN p_disposition;
END $$;

-- A per-return integrated read model. Security-invoker FUNCTION, not a PG14
-- security-definer view that could bypass row-level security.
CREATE FUNCTION one_return_case_overview(
  p_tenant uuid,p_return uuid,p_actor uuid
) RETURNS TABLE (
    return_id uuid, source_event_id uuid, return_kind text,
    warehouse_status text, finance_status text, requested_qty numeric,
    accepted_qty numeric, disposition_qty numeric,
    in_quarantine_qty numeric, credited_qty numeric,
    original_order_id uuid
) LANGUAGE plpgsql STABLE SECURITY INVOKER AS $$
BEGIN
  IF p_tenant IS NULL OR p_return IS NULL OR p_actor IS NULL
    OR current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text
    OR current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text
    OR NOT EXISTS(SELECT 1 FROM one_memberships WHERE tenant_id=p_tenant
      AND actor_id=p_actor AND active
      AND role IN ('company_admin','accountant','viewer')) THEN
    RAISE EXCEPTION 'Company return read permission required';
  END IF;
  RETURN QUERY
  SELECT r.id,r.source_event_id,r.return_kind,
    CASE WHEN r.status='requested' THEN 'inspection_pending'
         WHEN r.status='rejected' THEN 'rejected'
         WHEN r.return_kind='vendor' THEN 'vendor_stock_returned'
         WHEN d.disposition_total>=r.accepted_qty THEN 'qa_resolved'
         WHEN d.disposition_total>0 THEN 'qa_partially_resolved'
         ELSE 'quarantine' END,
    CASE WHEN r.return_kind='vendor' THEN 'supplier_bill_matching_not_ready'
         WHEN r.status='requested' THEN 'awaiting_physical_review'
         WHEN r.status='rejected' THEN 'not_due'
         WHEN i.invoiced_total=0 THEN 'not_invoiced'
         WHEN c.credit_total>=r.accepted_qty THEN 'credited'
         WHEN c.credit_total>0 THEN 'partially_credited'
         ELSE 'credit_review_pending' END,
    r.quantity,coalesce(r.accepted_qty,0),d.disposition_total,
    CASE WHEN r.return_kind='customer' AND r.status='completed'
         THEN greatest(r.accepted_qty-d.disposition_total,0)::numeric
         ELSE 0::numeric END,
    c.credit_total, s.order_id
  FROM one_return_jobs r
  JOIN one_stock_events s
    ON s.tenant_id=r.tenant_id AND s.id=r.source_event_id
  CROSS JOIN LATERAL (
    SELECT coalesce(sum(quantity),0)::numeric AS disposition_total
    FROM one_return_dispositions
    WHERE tenant_id=r.tenant_id AND return_job_id=r.id
  ) d
  CROSS JOIN LATERAL (
    SELECT coalesce(sum(quantity),0)::numeric AS credit_total
    FROM one_customer_return_credits
    WHERE tenant_id=r.tenant_id AND return_job_id=r.id
  ) c
  CROSS JOIN LATERAL (
    SELECT coalesce(sum(billed_qty),0)::numeric AS invoiced_total
    FROM one_sales_invoice_allocations
    WHERE tenant_id=r.tenant_id AND order_id=s.order_id
      AND order_line_no=s.line_no
  ) i
  WHERE r.tenant_id=p_tenant AND r.id=p_return;
END $$;

-- Existing event outbox must accept QA disposition movement kinds.
CREATE OR REPLACE FUNCTION one_route_operation_event() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE v_kind text;v_source uuid;
BEGIN
  IF TG_TABLE_NAME='one_stock_events' THEN
    v_kind=CASE NEW.movement_kind
      WHEN 'purchase_receipt' THEN 'stock.purchase_received'
      WHEN 'sale_dispatch' THEN 'stock.sale_dispatched'
      WHEN 'customer_return' THEN 'stock.customer_returned'
      WHEN 'vendor_return' THEN 'stock.vendor_returned'
      WHEN 'return_restock_out' THEN 'stock.return_restock_out'
      WHEN 'return_restock_in' THEN 'stock.return_restock_in'
      WHEN 'return_disposal' THEN 'stock.return_disposal'
      ELSE NULL END;v_source=NEW.id;
  ELSIF TG_TABLE_NAME='one_fulfillment_audit' THEN
    v_kind='delivery.'||NEW.event;v_source=NEW.delivery_id;
  ELSIF TG_TABLE_NAME='one_receiving_audit' THEN
    v_kind='receiving.'||NEW.event;v_source=NEW.inspection_id;
  ELSIF TG_TABLE_NAME='one_return_audit' THEN
    v_kind='returns.'||NEW.event;v_source=NEW.job_id;
  ELSIF TG_TABLE_NAME='one_invoice_audit' THEN
    v_kind='finance.invoice_issued';v_source=NEW.doc_id;
  ELSIF TG_TABLE_NAME='one_ledger_audit' THEN
    v_kind='finance.journal_posted';v_source=NEW.entry_id;
  ELSE
    RAISE EXCEPTION 'Unsupported event producer';
  END IF;
  IF v_kind IS NULL THEN RAISE EXCEPTION 'Unsupported event kind'; END IF;
  INSERT INTO one_operation_outbox(tenant_id,event_type,source_id)
    VALUES(NEW.tenant_id,v_kind,v_source);
  RETURN NEW;
END $$;

REVOKE ALL ON one_return_dispositions FROM PUBLIC;
REVOKE ALL ON FUNCTION
  one_resolve_return_quarantine(uuid,uuid,uuid,text,numeric,uuid,uuid,uuid,uuid)
  FROM PUBLIC;
REVOKE ALL ON FUNCTION one_return_case_overview(uuid,uuid,uuid) FROM PUBLIC;
COMMIT;
