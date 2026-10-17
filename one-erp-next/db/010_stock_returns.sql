-- ONE ERP Next / 010: traceable returns, quarantine and supplier return dispatch.
-- DEVELOPMENT / DISPOSABLE PostgreSQL 14 CI ONLY. NOT a production-return API.
-- Customer returns go to explicit quarantine, never immediately back to saleable.
BEGIN;
ALTER TABLE one_warehouses ADD COLUMN warehouse_type text NOT NULL DEFAULT 'sellable'
  CHECK (warehouse_type IN ('sellable','quarantine'));
ALTER TABLE one_stock_events DROP CONSTRAINT one_stock_events_movement_kind_check;
ALTER TABLE one_stock_events ADD CONSTRAINT one_stock_events_movement_kind_check
  CHECK (movement_kind IN (
    'purchase_receipt','sale_dispatch','customer_return','vendor_return'
  ));
ALTER TABLE one_operation_outbox DROP CONSTRAINT one_operation_outbox_event_type_check;
ALTER TABLE one_operation_outbox ADD CONSTRAINT one_operation_outbox_event_type_check
  CHECK (event_type IN (
    'stock.purchase_received','stock.sale_dispatched',
    'stock.customer_returned','stock.vendor_returned',
    'delivery.created','delivery.picked','delivery.packed',
    'delivery.dispatched','delivery.delivered',
    'receiving.inspection_started','receiving.inspection_completed',
    'returns.requested','returns.completed','returns.rejected',
    'finance.invoice_issued','finance.journal_posted'
  ));

CREATE TABLE one_return_jobs (
  tenant_id uuid NOT NULL REFERENCES one_tenants(id),
  id uuid NOT NULL,
  source_event_id uuid NOT NULL,
  return_kind text NOT NULL CHECK (return_kind IN ('customer','vendor')),
  quantity numeric(20,3) NOT NULL CHECK (quantity > 0 AND quantity::text<>'NaN'),
  accepted_qty numeric(20,3),
  destination_warehouse_id uuid,
  reason text NOT NULL CHECK (length(btrim(reason)) BETWEEN 3 AND 500),
  status text NOT NULL DEFAULT 'requested'
    CHECK (status IN ('requested','completed','rejected')),
  processed_event_id uuid,
  requested_by uuid NOT NULL,
  reviewed_by uuid,
  requested_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
  reviewed_at timestamptz,
  PRIMARY KEY (tenant_id,id),
  FOREIGN KEY (tenant_id,source_event_id)
    REFERENCES one_stock_events(tenant_id,id),
  FOREIGN KEY (tenant_id,processed_event_id)
    REFERENCES one_stock_events(tenant_id,id)
    DEFERRABLE INITIALLY DEFERRED,
  FOREIGN KEY (tenant_id,destination_warehouse_id)
    REFERENCES one_warehouses(tenant_id,id),
  CHECK (
    (return_kind='customer' AND destination_warehouse_id IS NOT NULL)
    OR (return_kind='vendor' AND destination_warehouse_id IS NULL)
  ),
  CHECK (
    (status='requested' AND accepted_qty IS NULL AND
      processed_event_id IS NULL AND reviewed_by IS NULL AND reviewed_at IS NULL)
    OR (status='rejected' AND accepted_qty=0 AND
      processed_event_id IS NULL AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)
    OR (status='completed' AND accepted_qty>0 AND accepted_qty<=quantity
      AND processed_event_id IS NOT NULL AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)
  )
);
CREATE INDEX one_returns_by_source
  ON one_return_jobs(tenant_id,source_event_id,status);
ALTER TABLE one_return_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_return_jobs FORCE ROW LEVEL SECURITY;
CREATE POLICY one_return_scope ON one_return_jobs
  USING (tenant_id::text=current_setting('one.tenant_id',true))
  WITH CHECK (tenant_id::text=current_setting('one.tenant_id',true));

CREATE TABLE one_return_audit (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  job_id uuid NOT NULL,
  event text NOT NULL CHECK (event IN ('requested','completed','rejected')),
  actor_id uuid NOT NULL,
  occurred_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
  PRIMARY KEY (tenant_id,id),
  UNIQUE (tenant_id,job_id,event),
  FOREIGN KEY (tenant_id,job_id) REFERENCES one_return_jobs(tenant_id,id)
);
ALTER TABLE one_return_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_return_audit FORCE ROW LEVEL SECURITY;
CREATE POLICY one_return_audit_scope ON one_return_audit
  USING (tenant_id::text=current_setting('one.tenant_id',true))
  WITH CHECK (tenant_id::text=current_setting('one.tenant_id',true));
CREATE TRIGGER one_return_audit_immutable BEFORE UPDATE OR DELETE
  ON one_return_audit FOR EACH ROW EXECUTE FUNCTION one_protect_audit();

CREATE FUNCTION one_return_job_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP='DELETE' OR current_setting('one.return_context',true)
     IS DISTINCT FROM 'yes' THEN
    RAISE EXCEPTION 'Return changes require controlled backend operation';
  END IF;
  IF TG_OP='INSERT' THEN
    IF NEW.status<>'requested' THEN RAISE EXCEPTION 'Return starts requested'; END IF;
    RETURN NEW;
  END IF;
  IF OLD.status<>'requested' OR NEW.status NOT IN ('completed','rejected')
     OR ROW(NEW.tenant_id,NEW.id,NEW.source_event_id,NEW.return_kind,
            NEW.quantity,NEW.destination_warehouse_id,NEW.reason,
            NEW.requested_by,NEW.requested_at)
        IS DISTINCT FROM
        ROW(OLD.tenant_id,OLD.id,OLD.source_event_id,OLD.return_kind,
            OLD.quantity,OLD.destination_warehouse_id,OLD.reason,
            OLD.requested_by,OLD.requested_at) THEN
    RAISE EXCEPTION 'Return identity immutable and no repeated processing';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER one_return_job_guard BEFORE INSERT OR UPDATE OR DELETE
  ON one_return_jobs FOR EACH ROW EXECUTE FUNCTION one_return_job_guard();

-- Lock source order before source event in both entrypoints, consistent with
-- existing order/line/stock lock hierarchy. Source event quantity is an
-- immutable upper bound; open requests count against that original quantity.
CREATE FUNCTION one_request_return(
  p_tenant uuid,p_job uuid,p_source_event uuid,p_qty numeric(20,3),
  p_quarantine uuid,p_actor uuid,p_reason text
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE src one_stock_events%ROWTYPE; ord one_commercial_orders%ROWTYPE;
        used_qty numeric(24,3); quarantine_type text; quarantine_active boolean;
        source_order uuid;
BEGIN
  IF p_tenant IS NULL OR p_job IS NULL OR p_source_event IS NULL OR
     p_actor IS NULL OR p_qty IS NULL OR p_qty<=0 OR p_qty::text='NaN'
     OR p_reason IS NULL OR length(btrim(p_reason)) NOT BETWEEN 3 AND 500 OR
     current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
     current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
     NOT EXISTS(SELECT 1 FROM one_memberships WHERE tenant_id=p_tenant
        AND actor_id=p_actor AND active AND role='company_admin') THEN
    RAISE EXCEPTION 'Authorized positive return request required';
  END IF;
  SELECT order_id INTO source_order FROM one_stock_events
    WHERE tenant_id=p_tenant AND id=p_source_event;
  IF source_order IS NULL THEN RAISE EXCEPTION 'Unknown return source event'; END IF;
  SELECT * INTO ord FROM one_commercial_orders
    WHERE tenant_id=p_tenant AND id=source_order FOR UPDATE;
  IF NOT FOUND OR ord.status<>'confirmed' THEN
    RAISE EXCEPTION 'Confirmed source order required';
  END IF;
  SELECT * INTO src FROM one_stock_events
    WHERE tenant_id=p_tenant AND id=p_source_event FOR UPDATE;
  IF NOT FOUND OR src.movement_kind NOT IN ('sale_dispatch','purchase_receipt')
     OR (src.movement_kind='sale_dispatch' AND ord.kind<>'sale')
     OR (src.movement_kind='purchase_receipt' AND ord.kind<>'purchase') THEN
    RAISE EXCEPTION 'Only original dispatch or receipt can be returned';
  END IF;
  IF src.movement_kind='sale_dispatch' THEN
    SELECT warehouse_type,active INTO quarantine_type,quarantine_active
    FROM one_warehouses
    WHERE tenant_id=p_tenant AND id=p_quarantine;
    IF p_quarantine IS NULL OR quarantine_type IS DISTINCT FROM 'quarantine'
       OR quarantine_active IS DISTINCT FROM true
       OR p_quarantine=src.warehouse_id THEN
      RAISE EXCEPTION 'Customer return requires separate active quarantine';
    END IF;
  ELSIF p_quarantine IS NOT NULL THEN
    RAISE EXCEPTION 'Vendor return ships from original receiving warehouse';
  END IF;
  SELECT coalesce(sum(CASE WHEN status='requested' THEN quantity
                      WHEN status='completed' THEN accepted_qty
                      ELSE 0 END),0) INTO used_qty
    FROM one_return_jobs
    WHERE tenant_id=p_tenant AND source_event_id=p_source_event;
  IF used_qty+p_qty>src.quantity THEN
    RAISE EXCEPTION 'Return quantity exceeds unreturned source quantity';
  END IF;
  PERFORM set_config('one.return_context','yes',true);
  INSERT INTO one_return_jobs
    (tenant_id,id,source_event_id,return_kind,quantity,
     destination_warehouse_id,reason,requested_by)
  VALUES(p_tenant,p_job,p_source_event,
    CASE WHEN src.movement_kind='sale_dispatch' THEN 'customer' ELSE 'vendor' END,
    p_qty,p_quarantine,btrim(p_reason),p_actor);
  INSERT INTO one_return_audit(tenant_id,job_id,event,actor_id)
    VALUES(p_tenant,p_job,'requested',p_actor);
  RETURN p_job;
END $$;

-- Approval controls the ONLY physical return movement. A declined/entirely
-- rejected return leaves source stock unchanged. Customer accepted goods go
-- straight into *quarantine*, never to regular available stock.
CREATE FUNCTION one_finish_return(
  p_tenant uuid,p_job uuid,p_accepted numeric(20,3),
  p_stock_event uuid,p_actor uuid
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE job one_return_jobs%ROWTYPE; src one_stock_events%ROWTYPE;
        source_order uuid; v_order one_commercial_orders%ROWTYPE;
        destination uuid; balance_row integer;
BEGIN
  IF p_tenant IS NULL OR p_job IS NULL OR p_actor IS NULL
     OR p_accepted IS NULL OR p_accepted<0 OR p_accepted::text='NaN'
     OR current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text
     OR current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text
     OR NOT EXISTS(SELECT 1 FROM one_memberships WHERE tenant_id=p_tenant
         AND actor_id=p_actor AND active AND role='company_admin') THEN
    RAISE EXCEPTION 'Authorized return inspection required';
  END IF;
  SELECT s.order_id INTO source_order FROM one_return_jobs j
    JOIN one_stock_events s ON s.tenant_id=j.tenant_id AND s.id=j.source_event_id
    WHERE j.tenant_id=p_tenant AND j.id=p_job;
  IF source_order IS NULL THEN RAISE EXCEPTION 'Unknown return job'; END IF;
  SELECT * INTO v_order FROM one_commercial_orders
    WHERE tenant_id=p_tenant AND id=source_order FOR UPDATE;
  SELECT * INTO job FROM one_return_jobs
    WHERE tenant_id=p_tenant AND id=p_job FOR UPDATE;
  IF NOT FOUND OR job.status<>'requested' OR p_accepted>job.quantity OR
     (p_accepted=0 AND p_stock_event IS NOT NULL) OR
     (p_accepted>0 AND p_stock_event IS NULL) OR
     (job.return_kind='vendor' AND p_accepted NOT IN (0,job.quantity)) THEN
    RAISE EXCEPTION 'Return quantity, status or event invalid';
  END IF;
  SELECT * INTO src FROM one_stock_events
    WHERE tenant_id=p_tenant AND id=job.source_event_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'Source stock movement no longer exists'; END IF;
  PERFORM set_config('one.return_context','yes',true);
  IF p_accepted>0 THEN
    IF EXISTS(SELECT 1 FROM one_stock_events
      WHERE tenant_id=p_tenant AND id=p_stock_event) THEN
      RAISE EXCEPTION 'Physical return stock event already exists';
    END IF;
    PERFORM set_config('one.commerce_context','yes',true);
    destination=CASE WHEN job.return_kind='customer'
      THEN job.destination_warehouse_id ELSE src.warehouse_id END;
    IF job.return_kind='customer' THEN
      IF NOT EXISTS(SELECT 1 FROM one_warehouses
          WHERE tenant_id=p_tenant AND id=destination
            AND warehouse_type='quarantine' AND active) THEN
        RAISE EXCEPTION 'Quarantine warehouse unavailable';
      END IF;
      INSERT INTO one_stock_balances(tenant_id,warehouse_id,product_id)
        VALUES(p_tenant,destination,src.product_id)
        ON CONFLICT (tenant_id,warehouse_id,product_id) DO NOTHING;
      UPDATE one_stock_balances
        SET on_hand=on_hand+p_accepted
        WHERE tenant_id=p_tenant AND warehouse_id=destination
          AND product_id=src.product_id;
    ELSE
      UPDATE one_stock_balances
        SET on_hand=on_hand-p_accepted
        WHERE tenant_id=p_tenant AND warehouse_id=destination
          AND product_id=src.product_id
          AND on_hand-reserved>=p_accepted;
      IF NOT FOUND THEN
        RAISE EXCEPTION 'Insufficient unreserved stock for vendor return';
      END IF;
    END IF;
    INSERT INTO one_stock_events
      (tenant_id,id,order_id,line_no,warehouse_id,product_id,
       movement_kind,quantity,actor_id)
    VALUES(p_tenant,p_stock_event,src.order_id,src.line_no,destination,
           src.product_id,
           CASE WHEN job.return_kind='customer' THEN 'customer_return'
             ELSE 'vendor_return' END,p_accepted,p_actor);
  END IF;
  UPDATE one_return_jobs
    SET status=CASE WHEN p_accepted>0 THEN 'completed' ELSE 'rejected' END,
        accepted_qty=p_accepted,processed_event_id=p_stock_event,
        reviewed_by=p_actor,reviewed_at=transaction_timestamp()
    WHERE tenant_id=p_tenant AND id=p_job;
  INSERT INTO one_return_audit(tenant_id,job_id,event,actor_id)
    VALUES(p_tenant,p_job,
      CASE WHEN p_accepted>0 THEN 'completed' ELSE 'rejected' END,p_actor);
  RETURN p_job;
END $$;

-- Extend the shared business event router, keeping event-source keys
-- consistent with previous immutable outbox consumer semantics.
CREATE OR REPLACE FUNCTION one_route_operation_event() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE v_kind text; v_source uuid;
BEGIN
  IF TG_TABLE_NAME='one_stock_events' THEN
    v_kind=CASE NEW.movement_kind
      WHEN 'purchase_receipt' THEN 'stock.purchase_received'
      WHEN 'sale_dispatch' THEN 'stock.sale_dispatched'
      WHEN 'customer_return' THEN 'stock.customer_returned'
      WHEN 'vendor_return' THEN 'stock.vendor_returned'
      ELSE NULL END;
    v_source=NEW.id;
  ELSIF TG_TABLE_NAME='one_fulfillment_audit' THEN
    v_kind='delivery.'||NEW.event;
    v_source=NEW.delivery_id;
  ELSIF TG_TABLE_NAME='one_receiving_audit' THEN
    v_kind='receiving.'||NEW.event;
    v_source=NEW.inspection_id;
  ELSIF TG_TABLE_NAME='one_return_audit' THEN
    v_kind='returns.'||NEW.event;
    v_source=NEW.job_id;
  ELSIF TG_TABLE_NAME='one_invoice_audit' THEN
    v_kind='finance.invoice_issued';
    v_source=NEW.doc_id;
  ELSIF TG_TABLE_NAME='one_ledger_audit' THEN
    v_kind='finance.journal_posted';
    v_source=NEW.entry_id;
  ELSE
    RAISE EXCEPTION 'Unsupported event producer';
  END IF;
  IF v_kind IS NULL THEN RAISE EXCEPTION 'Unsupported business event kind'; END IF;
  INSERT INTO one_operation_outbox(tenant_id,event_type,source_id)
    VALUES(NEW.tenant_id,v_kind,v_source);
  RETURN NEW;
END $$;
CREATE TRIGGER one_return_outbox AFTER INSERT ON one_return_audit
  FOR EACH ROW EXECUTE FUNCTION one_route_operation_event();

REVOKE ALL ON one_return_jobs,one_return_audit FROM PUBLIC;
REVOKE ALL ON FUNCTION
  one_request_return(uuid,uuid,uuid,numeric,uuid,uuid,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION
  one_finish_return(uuid,uuid,numeric,uuid,uuid) FROM PUBLIC;
COMMIT;
