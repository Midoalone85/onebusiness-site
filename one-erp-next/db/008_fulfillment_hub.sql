-- ONE ERP Next: warehouse outbound delivery and inbound receiving inspection.
-- DISPOSABLE DEVELOPMENT PostgreSQL 14 ONLY. No live integrations or users.
-- Signed identity + dedicated role/tenant databases must exist BEFORE release.
BEGIN;

CREATE TABLE one_delivery_jobs (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  order_id uuid NOT NULL,
  line_no integer NOT NULL CHECK (line_no > 0),
  quantity numeric(20,3) NOT NULL CHECK (quantity > 0),
  status text NOT NULL DEFAULT 'draft'
    CHECK (status IN ('draft','picked','packed','dispatched','delivered')),
  dispatch_event_id uuid,
  proof_reference text,
  delivered_at timestamptz,
  actor_id uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
  PRIMARY KEY (tenant_id,id),
  FOREIGN KEY (tenant_id,order_id,line_no)
    REFERENCES one_commercial_order_lines(tenant_id,order_id,line_no),
  FOREIGN KEY (tenant_id,dispatch_event_id)
    REFERENCES one_stock_events(tenant_id,id) DEFERRABLE INITIALLY DEFERRED,
  CHECK (
    (status IN ('draft','picked','packed')
      AND dispatch_event_id IS NULL AND proof_reference IS NULL AND delivered_at IS NULL)
    OR (status='dispatched' AND dispatch_event_id IS NOT NULL
      AND proof_reference IS NULL AND delivered_at IS NULL)
    OR (status='delivered' AND dispatch_event_id IS NOT NULL
      AND length(btrim(proof_reference)) BETWEEN 1 AND 128
      AND delivered_at IS NOT NULL)
  )
);
CREATE INDEX one_deliveries_by_order
  ON one_delivery_jobs(tenant_id,order_id,line_no,status);
ALTER TABLE one_delivery_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_delivery_jobs FORCE ROW LEVEL SECURITY;
CREATE POLICY one_delivery_scope ON one_delivery_jobs
  USING (tenant_id::text=current_setting('one.tenant_id',true))
  WITH CHECK (tenant_id::text=current_setting('one.tenant_id',true));

CREATE TABLE one_fulfillment_audit (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  delivery_id uuid NOT NULL,
  event text NOT NULL
    CHECK (event IN ('created','picked','packed','dispatched','delivered')),
  actor_id uuid NOT NULL,
  happened_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
  PRIMARY KEY (tenant_id,id),
  UNIQUE (tenant_id,delivery_id,event),
  FOREIGN KEY (tenant_id,delivery_id)
    REFERENCES one_delivery_jobs(tenant_id,id)
);
ALTER TABLE one_fulfillment_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_fulfillment_audit FORCE ROW LEVEL SECURITY;
CREATE POLICY one_fulfillment_audit_scope ON one_fulfillment_audit
  USING (tenant_id::text=current_setting('one.tenant_id',true))
  WITH CHECK (tenant_id::text=current_setting('one.tenant_id',true));
CREATE TRIGGER one_fulfillment_audit_immutable BEFORE UPDATE OR DELETE
  ON one_fulfillment_audit FOR EACH ROW EXECUTE FUNCTION one_protect_audit();

CREATE FUNCTION one_delivery_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP='DELETE' OR current_setting('one.delivery_context',true)
     IS DISTINCT FROM 'yes' THEN
    RAISE EXCEPTION 'Warehouse delivery changes require controlled transitions';
  END IF;
  IF TG_OP='INSERT' THEN
    IF NEW.status<>'draft' OR NEW.dispatch_event_id IS NOT NULL THEN
      RAISE EXCEPTION 'Delivery must start in draft';
    END IF;
    RETURN NEW;
  END IF;
  IF ROW(NEW.tenant_id,NEW.id,NEW.order_id,NEW.line_no,NEW.quantity,
         NEW.actor_id,NEW.created_at)
     IS DISTINCT FROM
     ROW(OLD.tenant_id,OLD.id,OLD.order_id,OLD.line_no,OLD.quantity,
         OLD.actor_id,OLD.created_at)
     OR NOT (
       (OLD.status='draft' AND NEW.status='picked') OR
       (OLD.status='picked' AND NEW.status='packed') OR
       (OLD.status='packed' AND NEW.status='dispatched') OR
       (OLD.status='dispatched' AND NEW.status='delivered')
     ) THEN
    RAISE EXCEPTION 'Invalid delivery status transition or identity alteration';
  END IF;
  IF OLD.dispatch_event_id IS NOT NULL AND
     NEW.dispatch_event_id IS DISTINCT FROM OLD.dispatch_event_id THEN
    RAISE EXCEPTION 'Existing dispatch linkage is immutable';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER one_delivery_guard BEFORE INSERT OR UPDATE OR DELETE
  ON one_delivery_jobs FOR EACH ROW EXECUTE FUNCTION one_delivery_guard();

-- Pending inbound inspections must not be bypassed by arbitrary receiving.
CREATE TABLE one_receiving_inspections (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  order_id uuid NOT NULL,
  line_no integer NOT NULL CHECK (line_no > 0),
  expected_qty numeric(20,3) NOT NULL CHECK (expected_qty > 0),
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','completed')),
  accepted_qty numeric(20,3),
  rejected_qty numeric(20,3),
  receipt_event_id uuid,
  actor_id uuid NOT NULL,
  completed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
  PRIMARY KEY (tenant_id,id),
  FOREIGN KEY (tenant_id,order_id,line_no)
    REFERENCES one_commercial_order_lines(tenant_id,order_id,line_no),
  FOREIGN KEY (tenant_id,receipt_event_id)
    REFERENCES one_stock_events(tenant_id,id) DEFERRABLE INITIALLY DEFERRED,
  CHECK (
    (status='pending' AND accepted_qty IS NULL AND rejected_qty IS NULL
      AND receipt_event_id IS NULL AND completed_at IS NULL)
    OR (status='completed' AND completed_at IS NOT NULL
      AND accepted_qty >= 0 AND rejected_qty >= 0
      AND accepted_qty+rejected_qty=expected_qty
      AND ((accepted_qty > 0 AND receipt_event_id IS NOT NULL)
           OR (accepted_qty=0 AND receipt_event_id IS NULL)))
  )
);
CREATE INDEX one_receiving_by_order
  ON one_receiving_inspections(tenant_id,order_id,line_no,status);
ALTER TABLE one_receiving_inspections ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_receiving_inspections FORCE ROW LEVEL SECURITY;
CREATE POLICY one_receiving_scope ON one_receiving_inspections
  USING (tenant_id::text=current_setting('one.tenant_id',true))
  WITH CHECK (tenant_id::text=current_setting('one.tenant_id',true));

CREATE TABLE one_receiving_audit (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL,
  inspection_id uuid NOT NULL,
  event text NOT NULL CHECK (event IN ('inspection_started','inspection_completed')),
  actor_id uuid NOT NULL,
  happened_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
  PRIMARY KEY (tenant_id,id),
  UNIQUE (tenant_id,inspection_id,event),
  FOREIGN KEY (tenant_id,inspection_id)
    REFERENCES one_receiving_inspections(tenant_id,id)
);
ALTER TABLE one_receiving_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_receiving_audit FORCE ROW LEVEL SECURITY;
CREATE POLICY one_receiving_audit_scope ON one_receiving_audit
  USING (tenant_id::text=current_setting('one.tenant_id',true))
  WITH CHECK (tenant_id::text=current_setting('one.tenant_id',true));
CREATE TRIGGER one_receiving_audit_immutable BEFORE UPDATE OR DELETE
  ON one_receiving_audit FOR EACH ROW EXECUTE FUNCTION one_protect_audit();

CREATE FUNCTION one_receiving_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP='DELETE' OR current_setting('one.receiving_context',true)
     IS DISTINCT FROM 'yes' THEN
    RAISE EXCEPTION 'Receiving inspection requires controlled transition';
  END IF;
  IF TG_OP='INSERT' THEN
    IF NEW.status<>'pending' THEN
      RAISE EXCEPTION 'Inspection must begin pending';
    END IF;
    RETURN NEW;
  END IF;
  IF OLD.status<>'pending' OR NEW.status<>'completed' OR
     ROW(NEW.tenant_id,NEW.id,NEW.order_id,NEW.line_no,NEW.expected_qty,
         NEW.actor_id,NEW.created_at)
     IS DISTINCT FROM
     ROW(OLD.tenant_id,OLD.id,OLD.order_id,OLD.line_no,OLD.expected_qty,
         OLD.actor_id,OLD.created_at) THEN
    RAISE EXCEPTION 'Completed inspection cannot be changed';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER one_receiving_guard BEFORE INSERT OR UPDATE OR DELETE
  ON one_receiving_inspections FOR EACH ROW EXECUTE FUNCTION one_receiving_guard();

-- The old raw release/dispatch paths must honor existing delivery allocations.
-- Conversely a direct receipt must not consume pending inspection allocations.
-- Both checks are on the shared commercial line, serialized by order locks.
CREATE FUNCTION one_fulfillment_allocation_guard()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE outbound_assigned numeric(20,3); inbound_pending numeric(20,3);
        v_kind text;
BEGIN
  SELECT kind INTO v_kind FROM one_commercial_orders
    WHERE tenant_id=NEW.tenant_id AND id=NEW.order_id;
  IF v_kind='sale' THEN
    SELECT coalesce(sum(quantity),0) INTO outbound_assigned
      FROM one_delivery_jobs
      WHERE tenant_id=NEW.tenant_id AND order_id=NEW.order_id
        AND line_no=NEW.line_no AND status IN ('draft','picked','packed');
    IF NEW.reserved_qty < outbound_assigned THEN
      RAISE EXCEPTION 'Cannot consume stock allocated to pending deliveries';
    END IF;
  ELSIF v_kind='purchase' THEN
    SELECT coalesce(sum(expected_qty),0) INTO inbound_pending
      FROM one_receiving_inspections
      WHERE tenant_id=NEW.tenant_id AND order_id=NEW.order_id
        AND line_no=NEW.line_no AND status='pending';
    IF NEW.fulfilled_qty + inbound_pending > NEW.ordered_qty THEN
      RAISE EXCEPTION 'Purchase order quantity committed to receiving inspection';
    END IF;
  END IF;
  RETURN NULL;
END $$;
CREATE TRIGGER one_fulfillment_allocation_guard
  AFTER UPDATE ON one_commercial_order_lines FOR EACH ROW
  EXECUTE FUNCTION one_fulfillment_allocation_guard();

-- Exactly one active delivery allocation per reserved physical quantity.
CREATE FUNCTION one_create_delivery(
  p_tenant uuid,p_job uuid,p_order uuid,p_line integer,
  p_qty numeric(20,3),p_actor uuid
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE o one_commercial_orders%ROWTYPE; l one_commercial_order_lines%ROWTYPE;
        allocated numeric(20,3);
BEGIN
  IF p_tenant IS NULL OR p_job IS NULL OR p_order IS NULL OR p_line IS NULL
    OR p_qty IS NULL OR p_qty<=0 OR p_actor IS NULL OR
    current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
    current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
    NOT EXISTS(SELECT 1 FROM one_memberships
      WHERE tenant_id=p_tenant AND actor_id=p_actor AND role='company_admin'
        AND active) THEN
    RAISE EXCEPTION 'Authorized warehouse administrator required';
  END IF;
  SELECT * INTO o FROM one_commercial_orders
    WHERE tenant_id=p_tenant AND id=p_order FOR UPDATE;
  IF NOT FOUND OR o.kind<>'sale' OR o.status<>'confirmed' THEN
    RAISE EXCEPTION 'Confirmed sales order required';
  END IF;
  SELECT * INTO l FROM one_commercial_order_lines
    WHERE tenant_id=p_tenant AND order_id=p_order AND line_no=p_line FOR UPDATE;
  IF NOT FOUND OR l.reserved_qty<p_qty THEN
    RAISE EXCEPTION 'Source line lacks reservation';
  END IF;
  SELECT coalesce(sum(quantity),0) INTO allocated FROM one_delivery_jobs
    WHERE tenant_id=p_tenant AND order_id=p_order AND line_no=p_line
      AND status IN ('draft','picked','packed');
  IF allocated+p_qty>l.reserved_qty THEN
    RAISE EXCEPTION 'Cannot allocate same reservation to two delivery jobs';
  END IF;
  PERFORM set_config('one.delivery_context','yes',true);
  INSERT INTO one_delivery_jobs
    (tenant_id,id,order_id,line_no,quantity,actor_id)
    VALUES (p_tenant,p_job,p_order,p_line,p_qty,p_actor);
  INSERT INTO one_fulfillment_audit
    (tenant_id,id,delivery_id,event,actor_id)
    VALUES (p_tenant,p_job,p_job,'created',p_actor);
  RETURN p_job;
END $$;

CREATE FUNCTION one_advance_delivery(
  p_tenant uuid,p_job uuid,p_next text,p_actor uuid,
  p_event uuid DEFAULT NULL,p_proof text DEFAULT NULL
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE job one_delivery_jobs%ROWTYPE; v_order uuid; v_status text;
BEGIN
  IF p_tenant IS NULL OR p_job IS NULL OR p_actor IS NULL OR
    p_next IS NULL OR
    current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
    current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
    NOT EXISTS(SELECT 1 FROM one_memberships
      WHERE tenant_id=p_tenant AND actor_id=p_actor AND role='company_admin'
        AND active) THEN
    RAISE EXCEPTION 'Authorized warehouse administrator required';
  END IF;
  SELECT order_id INTO v_order FROM one_delivery_jobs
    WHERE tenant_id=p_tenant AND id=p_job;
  IF v_order IS NULL THEN RAISE EXCEPTION 'Delivery job not found'; END IF;
  PERFORM 1 FROM one_commercial_orders
    WHERE tenant_id=p_tenant AND id=v_order FOR UPDATE;
  SELECT * INTO job FROM one_delivery_jobs
    WHERE tenant_id=p_tenant AND id=p_job FOR UPDATE;
  IF NOT FOUND OR NOT (
    (job.status='draft' AND p_next='picked') OR
    (job.status='picked' AND p_next='packed') OR
    (job.status='packed' AND p_next='dispatched') OR
    (job.status='dispatched' AND p_next='delivered')
  ) THEN
    RAISE EXCEPTION 'Delivery stage skipped, repeated or out of order';
  END IF;
  IF (p_next='dispatched' AND (p_event IS NULL OR p_proof IS NOT NULL)) OR
     (p_next='delivered' AND (p_event IS NOT NULL OR
        p_proof IS NULL OR length(btrim(p_proof)) NOT BETWEEN 1 AND 128)) OR
     (p_next IN ('picked','packed') AND
        (p_event IS NOT NULL OR p_proof IS NOT NULL)) THEN
    RAISE EXCEPTION 'Stage evidence fields invalid';
  END IF;
  PERFORM set_config('one.delivery_context','yes',true);
  IF p_next='dispatched' THEN
    -- FK on event is deferred until transaction commit; event is created next.
    UPDATE one_delivery_jobs
      SET status='dispatched',dispatch_event_id=p_event
      WHERE tenant_id=p_tenant AND id=p_job;
    PERFORM one_dispatch_reserved_sale_line(
      p_tenant,job.order_id,job.line_no,job.quantity,p_event,p_actor);
  ELSIF p_next='delivered' THEN
    UPDATE one_delivery_jobs SET status='delivered',
      proof_reference=p_proof,delivered_at=transaction_timestamp()
      WHERE tenant_id=p_tenant AND id=p_job;
  ELSE
    UPDATE one_delivery_jobs SET status=p_next
      WHERE tenant_id=p_tenant AND id=p_job;
  END IF;
  INSERT INTO one_fulfillment_audit
    (tenant_id,id,delivery_id,event,actor_id)
    VALUES (p_tenant,gen_random_uuid(),p_job,p_next,p_actor);
  RETURN p_job;
END $$;

CREATE FUNCTION one_start_receiving_inspection(
  p_tenant uuid,p_inspection uuid,p_order uuid,p_line integer,
  p_qty numeric(20,3),p_actor uuid
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE o one_commercial_orders%ROWTYPE; l one_commercial_order_lines%ROWTYPE;
        pending_qty numeric(20,3);
BEGIN
  IF p_tenant IS NULL OR p_inspection IS NULL OR p_order IS NULL OR
     p_line IS NULL OR p_qty IS NULL OR p_qty<=0 OR p_actor IS NULL OR
     current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
     current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
     NOT EXISTS(SELECT 1 FROM one_memberships
       WHERE tenant_id=p_tenant AND actor_id=p_actor AND role='company_admin'
         AND active) THEN
    RAISE EXCEPTION 'Authorized receiving administrator required';
  END IF;
  SELECT * INTO o FROM one_commercial_orders
    WHERE tenant_id=p_tenant AND id=p_order FOR UPDATE;
  IF NOT FOUND OR o.kind<>'purchase' OR o.status<>'confirmed' THEN
    RAISE EXCEPTION 'Confirmed purchase order required';
  END IF;
  SELECT * INTO l FROM one_commercial_order_lines
    WHERE tenant_id=p_tenant AND order_id=p_order AND line_no=p_line FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'Purchase line missing'; END IF;
  SELECT coalesce(sum(expected_qty),0) INTO pending_qty
    FROM one_receiving_inspections
    WHERE tenant_id=p_tenant AND order_id=p_order AND line_no=p_line
      AND status='pending';
  IF p_qty+pending_qty>l.ordered_qty-l.fulfilled_qty THEN
    RAISE EXCEPTION 'Inspection exceeds open purchase quantity';
  END IF;
  PERFORM set_config('one.receiving_context','yes',true);
  INSERT INTO one_receiving_inspections
    (tenant_id,id,order_id,line_no,expected_qty,actor_id)
    VALUES (p_tenant,p_inspection,p_order,p_line,p_qty,p_actor);
  INSERT INTO one_receiving_audit
    (tenant_id,id,inspection_id,event,actor_id)
    VALUES (p_tenant,p_inspection,p_inspection,'inspection_started',p_actor);
  RETURN p_inspection;
END $$;

CREATE FUNCTION one_complete_receiving_inspection(
  p_tenant uuid,p_inspection uuid,p_accepted numeric(20,3),
  p_rejected numeric(20,3),p_actor uuid,p_stock_event uuid DEFAULT NULL
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE src one_receiving_inspections%ROWTYPE; v_order uuid;
BEGIN
  IF p_tenant IS NULL OR p_inspection IS NULL OR p_actor IS NULL OR
     p_accepted IS NULL OR p_rejected IS NULL OR p_accepted<0 OR
     p_rejected<0 OR
     current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
     current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
     NOT EXISTS(SELECT 1 FROM one_memberships
       WHERE tenant_id=p_tenant AND actor_id=p_actor AND role='company_admin'
         AND active) THEN
    RAISE EXCEPTION 'Authorized inspection completion required';
  END IF;
  SELECT order_id INTO v_order FROM one_receiving_inspections
    WHERE tenant_id=p_tenant AND id=p_inspection;
  IF v_order IS NULL THEN RAISE EXCEPTION 'Inspection not found'; END IF;
  PERFORM 1 FROM one_commercial_orders
    WHERE tenant_id=p_tenant AND id=v_order FOR UPDATE;
  SELECT * INTO src FROM one_receiving_inspections
    WHERE tenant_id=p_tenant AND id=p_inspection FOR UPDATE;
  IF NOT FOUND OR src.status<>'pending'
     OR src.expected_qty<>p_accepted+p_rejected
     OR (p_accepted>0 AND p_stock_event IS NULL)
     OR (p_accepted=0 AND p_stock_event IS NOT NULL) THEN
    RAISE EXCEPTION 'Invalid or repeated inspection completion';
  END IF;
  PERFORM set_config('one.receiving_context','yes',true);
  UPDATE one_receiving_inspections SET
    status='completed',accepted_qty=p_accepted,rejected_qty=p_rejected,
    receipt_event_id=p_stock_event,completed_at=transaction_timestamp()
    WHERE tenant_id=p_tenant AND id=p_inspection;
  IF p_accepted>0 THEN
    PERFORM one_fulfill_commercial_line(
      p_tenant,src.order_id,src.line_no,p_accepted,p_stock_event,p_actor);
  END IF;
  INSERT INTO one_receiving_audit
    (tenant_id,id,inspection_id,event,actor_id)
    VALUES (p_tenant,gen_random_uuid(),p_inspection,'inspection_completed',p_actor);
  RETURN p_inspection;
END $$;

REVOKE ALL ON one_delivery_jobs,one_fulfillment_audit,
  one_receiving_inspections,one_receiving_audit FROM PUBLIC;
REVOKE ALL ON FUNCTION
  one_create_delivery(uuid,uuid,uuid,integer,numeric,uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION
  one_advance_delivery(uuid,uuid,text,uuid,uuid,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION
  one_start_receiving_inspection(uuid,uuid,uuid,integer,numeric,uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION
  one_complete_receiving_inspection(uuid,uuid,numeric,numeric,uuid,uuid) FROM PUBLIC;
COMMIT;
