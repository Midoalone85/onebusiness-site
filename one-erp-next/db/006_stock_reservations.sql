-- ONE ERP Next: per-order-line reservations and reserved dispatch.
-- PostgreSQL 14+; CI / development only. NOT an authenticated production API.
-- No Oracle changes. Backend-only tenant+actor context is NOT authentication.
BEGIN;

ALTER TABLE one_commercial_order_lines
  ADD COLUMN reserved_qty numeric(20,3) NOT NULL DEFAULT 0
  CHECK (reserved_qty >= 0 AND fulfilled_qty + reserved_qty <= ordered_qty);

CREATE TABLE one_reservation_events (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL,
    order_id uuid NOT NULL,
    line_no integer NOT NULL,
    action text NOT NULL CHECK (action IN ('hold','release')),
    quantity numeric(20,3) NOT NULL CHECK (quantity > 0),
    actor_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    PRIMARY KEY (tenant_id,id),
    FOREIGN KEY (tenant_id,order_id,line_no)
      REFERENCES one_commercial_order_lines(tenant_id,order_id,line_no)
);
CREATE INDEX one_reservation_events_by_line
  ON one_reservation_events(tenant_id,order_id,line_no,created_at);

ALTER TABLE one_reservation_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_reservation_events FORCE ROW LEVEL SECURITY;
CREATE POLICY one_reservation_events_tenant ON one_reservation_events
  USING (tenant_id::text=current_setting('one.tenant_id',true))
  WITH CHECK (tenant_id::text=current_setting('one.tenant_id',true));

CREATE TRIGGER one_reservation_events_immutable BEFORE UPDATE OR DELETE
  ON one_reservation_events FOR EACH ROW
  EXECUTE FUNCTION one_stock_event_immutable();

-- Extend old 005 trigger without weakening confirmed order identity immutability.
-- The session commerce flag only scopes internal writes: it is not a login check.
CREATE OR REPLACE FUNCTION one_commercial_line_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE v_status text;
BEGIN
    SELECT status INTO v_status
      FROM one_commercial_orders
     WHERE tenant_id = CASE WHEN TG_OP='DELETE' THEN OLD.tenant_id ELSE NEW.tenant_id END
       AND id = CASE WHEN TG_OP='DELETE' THEN OLD.order_id ELSE NEW.order_id END
     FOR UPDATE;
    IF v_status IS NULL THEN
        RAISE EXCEPTION 'Parent commercial order required';
    END IF;
    IF v_status='draft' THEN
        IF TG_OP<>'INSERT' AND
           (OLD.fulfilled_qty<>0 OR OLD.reserved_qty<>0) THEN
            RAISE EXCEPTION 'Allocated draft line cannot be changed';
        END IF;
        IF TG_OP<>'DELETE' AND
           (NEW.fulfilled_qty<>0 OR NEW.reserved_qty<>0) THEN
            RAISE EXCEPTION 'Draft lines must start unfulfilled and unreserved';
        END IF;
        IF TG_OP='DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
    END IF;
    IF TG_OP<>'UPDATE' OR
       current_setting('one.commerce_context',true) IS DISTINCT FROM 'yes' OR
       NEW.fulfilled_qty < OLD.fulfilled_qty OR
       (NEW.fulfilled_qty=OLD.fulfilled_qty AND
        NEW.reserved_qty=OLD.reserved_qty) OR
       ROW(NEW.tenant_id,NEW.order_id,NEW.line_no,NEW.product_id,
           NEW.warehouse_id,NEW.ordered_qty,NEW.unit_price)
       IS DISTINCT FROM
       ROW(OLD.tenant_id,OLD.order_id,OLD.line_no,OLD.product_id,
           OLD.warehouse_id,OLD.ordered_qty,OLD.unit_price) THEN
        RAISE EXCEPTION 'Confirmed commercial line may only have trusted allocations';
    END IF;
    RETURN NEW;
END $$;

CREATE FUNCTION one_reserve_sale_line(
    p_tenant uuid, p_order uuid, p_line_no integer,
    p_quantity numeric(20,3), p_event uuid, p_actor uuid
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE
    o one_commercial_orders%ROWTYPE;
    l one_commercial_order_lines%ROWTYPE;
BEGIN
    IF p_tenant IS NULL OR p_order IS NULL OR p_line_no IS NULL OR
       p_quantity IS NULL OR p_quantity<=0 OR p_event IS NULL OR p_actor IS NULL OR
       current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
       current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
       NOT EXISTS (
           SELECT 1 FROM one_memberships
           WHERE tenant_id=p_tenant AND actor_id=p_actor AND active
             AND role='company_admin'
       ) THEN
        RAISE EXCEPTION 'Authorized positive reservation required';
    END IF;
    SELECT * INTO o FROM one_commercial_orders
      WHERE tenant_id=p_tenant AND id=p_order FOR UPDATE;
    IF NOT FOUND OR o.status<>'confirmed' OR o.kind<>'sale' THEN
        RAISE EXCEPTION 'Confirmed sales order required';
    END IF;
    SELECT * INTO l FROM one_commercial_order_lines
      WHERE tenant_id=p_tenant AND order_id=p_order AND line_no=p_line_no
      FOR UPDATE;
    IF NOT FOUND OR l.ordered_qty-l.fulfilled_qty-l.reserved_qty<p_quantity THEN
        RAISE EXCEPTION 'Reservation exceeds open order line quantity';
    END IF;
    IF EXISTS (SELECT 1 FROM one_reservation_events
               WHERE tenant_id=p_tenant AND id=p_event) THEN
        RAISE EXCEPTION 'Reservation event already recorded';
    END IF;
    IF NOT EXISTS(SELECT 1 FROM one_products
                  WHERE tenant_id=p_tenant AND id=l.product_id AND active)
       OR NOT EXISTS(SELECT 1 FROM one_warehouses
                     WHERE tenant_id=p_tenant AND id=l.warehouse_id AND active) THEN
        RAISE EXCEPTION 'Inactive catalog entity';
    END IF;
    PERFORM set_config('one.commerce_context','yes',true);
    UPDATE one_stock_balances
       SET reserved=reserved+p_quantity
     WHERE tenant_id=p_tenant AND warehouse_id=l.warehouse_id
       AND product_id=l.product_id
       AND on_hand-reserved>=p_quantity;
    IF NOT FOUND THEN RAISE EXCEPTION 'Insufficient unreserved stock'; END IF;
    UPDATE one_commercial_order_lines
       SET reserved_qty=reserved_qty+p_quantity
     WHERE tenant_id=p_tenant AND order_id=p_order AND line_no=p_line_no;
    INSERT INTO one_reservation_events
       (tenant_id,id,order_id,line_no,action,quantity,actor_id)
    VALUES (p_tenant,p_event,p_order,p_line_no,'hold',p_quantity,p_actor);
    RETURN p_event;
END $$;

CREATE FUNCTION one_release_sale_line(
    p_tenant uuid, p_order uuid, p_line_no integer,
    p_quantity numeric(20,3), p_event uuid, p_actor uuid
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE
    o one_commercial_orders%ROWTYPE;
    l one_commercial_order_lines%ROWTYPE;
BEGIN
    IF p_tenant IS NULL OR p_order IS NULL OR p_line_no IS NULL OR
       p_quantity IS NULL OR p_quantity<=0 OR p_event IS NULL OR p_actor IS NULL OR
       current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
       current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
       NOT EXISTS (
           SELECT 1 FROM one_memberships
           WHERE tenant_id=p_tenant AND actor_id=p_actor AND active
             AND role='company_admin'
       ) THEN
        RAISE EXCEPTION 'Authorized positive release required';
    END IF;
    SELECT * INTO o FROM one_commercial_orders
      WHERE tenant_id=p_tenant AND id=p_order FOR UPDATE;
    IF NOT FOUND OR o.status<>'confirmed' OR o.kind<>'sale' THEN
        RAISE EXCEPTION 'Confirmed sales order required';
    END IF;
    SELECT * INTO l FROM one_commercial_order_lines
      WHERE tenant_id=p_tenant AND order_id=p_order AND line_no=p_line_no
      FOR UPDATE;
    IF NOT FOUND OR l.reserved_qty<p_quantity THEN
        RAISE EXCEPTION 'Cannot release stock not held by this sales line';
    END IF;
    IF EXISTS (SELECT 1 FROM one_reservation_events
               WHERE tenant_id=p_tenant AND id=p_event) THEN
        RAISE EXCEPTION 'Reservation event already recorded';
    END IF;
    PERFORM set_config('one.commerce_context','yes',true);
    UPDATE one_stock_balances SET reserved=reserved-p_quantity
     WHERE tenant_id=p_tenant AND warehouse_id=l.warehouse_id
       AND product_id=l.product_id AND reserved>=p_quantity;
    IF NOT FOUND THEN RAISE EXCEPTION 'Missing stock reservation balance'; END IF;
    UPDATE one_commercial_order_lines
       SET reserved_qty=reserved_qty-p_quantity
     WHERE tenant_id=p_tenant AND order_id=p_order AND line_no=p_line_no;
    INSERT INTO one_reservation_events
       (tenant_id,id,order_id,line_no,action,quantity,actor_id)
    VALUES (p_tenant,p_event,p_order,p_line_no,'release',p_quantity,p_actor);
    RETURN p_event;
END $$;

-- Explicitly dispatch THIS line's held stock. The original unreserved
-- dispatch function remains valid and cannot steal stock from other orders.
CREATE FUNCTION one_dispatch_reserved_sale_line(
    p_tenant uuid, p_order uuid, p_line_no integer,
    p_quantity numeric(20,3), p_event uuid, p_actor uuid
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE
    o one_commercial_orders%ROWTYPE;
    l one_commercial_order_lines%ROWTYPE;
BEGIN
    IF p_tenant IS NULL OR p_order IS NULL OR p_line_no IS NULL OR
       p_quantity IS NULL OR p_quantity<=0 OR p_event IS NULL OR p_actor IS NULL OR
       current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
       current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
       NOT EXISTS (
           SELECT 1 FROM one_memberships
           WHERE tenant_id=p_tenant AND actor_id=p_actor AND active
             AND role='company_admin'
       ) THEN
        RAISE EXCEPTION 'Authorized positive reserved dispatch required';
    END IF;
    SELECT * INTO o FROM one_commercial_orders
      WHERE tenant_id=p_tenant AND id=p_order FOR UPDATE;
    IF NOT FOUND OR o.status<>'confirmed' OR o.kind<>'sale' THEN
        RAISE EXCEPTION 'Confirmed sales order required';
    END IF;
    SELECT * INTO l FROM one_commercial_order_lines
      WHERE tenant_id=p_tenant AND order_id=p_order AND line_no=p_line_no
      FOR UPDATE;
    IF NOT FOUND OR l.reserved_qty<p_quantity
       OR l.ordered_qty-l.fulfilled_qty<p_quantity THEN
        RAISE EXCEPTION 'Cannot dispatch more than this line reserved';
    END IF;
    IF EXISTS(SELECT 1 FROM one_stock_events
              WHERE tenant_id=p_tenant AND id=p_event) THEN
        RAISE EXCEPTION 'Stock event already recorded';
    END IF;
    IF NOT EXISTS(SELECT 1 FROM one_products
                  WHERE tenant_id=p_tenant AND id=l.product_id AND active)
       OR NOT EXISTS(SELECT 1 FROM one_warehouses
                     WHERE tenant_id=p_tenant AND id=l.warehouse_id AND active) THEN
        RAISE EXCEPTION 'Inactive catalog entity cannot dispatch';
    END IF;
    PERFORM set_config('one.commerce_context','yes',true);
    UPDATE one_stock_balances
       SET on_hand=on_hand-p_quantity, reserved=reserved-p_quantity
     WHERE tenant_id=p_tenant AND warehouse_id=l.warehouse_id
       AND product_id=l.product_id
       AND on_hand>=p_quantity AND reserved>=p_quantity;
    IF NOT FOUND THEN RAISE EXCEPTION 'Reserved stock balance insufficient'; END IF;
    UPDATE one_commercial_order_lines
       SET fulfilled_qty=fulfilled_qty+p_quantity,
           reserved_qty=reserved_qty-p_quantity
     WHERE tenant_id=p_tenant AND order_id=p_order AND line_no=p_line_no;
    INSERT INTO one_stock_events
       (tenant_id,id,order_id,line_no,warehouse_id,product_id,
        movement_kind,quantity,actor_id)
    VALUES (p_tenant,p_event,p_order,p_line_no,l.warehouse_id,l.product_id,
            'sale_dispatch',p_quantity,p_actor);
    RETURN p_event;
END $$;

REVOKE ALL ON FUNCTION
  one_reserve_sale_line(uuid,uuid,integer,numeric,uuid,uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION
  one_release_sale_line(uuid,uuid,integer,numeric,uuid,uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION
  one_dispatch_reserved_sale_line(uuid,uuid,integer,numeric,uuid,uuid) FROM PUBLIC;

COMMIT;
