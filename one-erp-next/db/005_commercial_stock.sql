-- ONE ERP Next independent sales/purchasing/physical stock foundation.
-- PostgreSQL 14+ / DISPOSABLE CI ONLY until reviewed production grants.
-- This migration adds no invoicing, valuation, tax compliance or live POS.
-- one.tenant_id and one.actor_id are backend-only context, NOT authentication.
BEGIN;

CREATE TABLE one_products (
    tenant_id uuid NOT NULL REFERENCES one_tenants(id),
    id uuid NOT NULL,
    sku text NOT NULL CHECK (length(btrim(sku)) BETWEEN 1 AND 80),
    name text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 256),
    active boolean NOT NULL DEFAULT true,
    PRIMARY KEY (tenant_id,id),
    UNIQUE (tenant_id,sku)
);
CREATE TABLE one_warehouses (
    tenant_id uuid NOT NULL REFERENCES one_tenants(id),
    id uuid NOT NULL,
    code text NOT NULL CHECK (length(btrim(code)) BETWEEN 1 AND 40),
    name text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 256),
    active boolean NOT NULL DEFAULT true,
    PRIMARY KEY (tenant_id,id),
    UNIQUE (tenant_id,code)
);
CREATE TABLE one_stock_balances (
    tenant_id uuid NOT NULL,
    warehouse_id uuid NOT NULL,
    product_id uuid NOT NULL,
    on_hand numeric(20,3) NOT NULL DEFAULT 0 CHECK (on_hand >= 0),
    reserved numeric(20,3) NOT NULL DEFAULT 0 CHECK (reserved >= 0),
    PRIMARY KEY (tenant_id,warehouse_id,product_id),
    FOREIGN KEY (tenant_id,warehouse_id)
        REFERENCES one_warehouses(tenant_id,id),
    FOREIGN KEY (tenant_id,product_id)
        REFERENCES one_products(tenant_id,id),
    CHECK (reserved <= on_hand)
);
CREATE TABLE one_commercial_orders (
    tenant_id uuid NOT NULL REFERENCES one_tenants(id),
    id uuid NOT NULL,
    kind text NOT NULL CHECK (kind IN ('sale','purchase')),
    number text NOT NULL CHECK (length(btrim(number)) BETWEEN 1 AND 100),
    partner_id text NOT NULL CHECK (length(btrim(partner_id)) BETWEEN 1 AND 128),
    currency text NOT NULL REFERENCES one_currency_precision(code),
    status text NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft','confirmed')),
    confirmed_at timestamptz,
    confirmed_by uuid,
    PRIMARY KEY (tenant_id,id),
    UNIQUE (tenant_id,kind,number),
    CHECK ((status='draft' AND confirmed_at IS NULL AND confirmed_by IS NULL)
        OR (status='confirmed' AND confirmed_at IS NOT NULL
            AND confirmed_by IS NOT NULL))
);
CREATE TABLE one_commercial_order_lines (
    tenant_id uuid NOT NULL,
    order_id uuid NOT NULL,
    line_no integer NOT NULL CHECK (line_no>0),
    product_id uuid NOT NULL,
    warehouse_id uuid NOT NULL,
    ordered_qty numeric(20,3) NOT NULL CHECK (ordered_qty>0),
    fulfilled_qty numeric(20,3) NOT NULL DEFAULT 0
        CHECK (fulfilled_qty>=0 AND fulfilled_qty<=ordered_qty),
    unit_price numeric(20,3) NOT NULL CHECK (unit_price>=0),
    PRIMARY KEY (tenant_id,order_id,line_no),
    FOREIGN KEY (tenant_id,order_id)
        REFERENCES one_commercial_orders(tenant_id,id),
    FOREIGN KEY (tenant_id,product_id)
        REFERENCES one_products(tenant_id,id),
    FOREIGN KEY (tenant_id,warehouse_id)
        REFERENCES one_warehouses(tenant_id,id)
);
CREATE TABLE one_stock_events (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL,
    order_id uuid NOT NULL,
    line_no integer NOT NULL,
    warehouse_id uuid NOT NULL,
    product_id uuid NOT NULL,
    movement_kind text NOT NULL
        CHECK (movement_kind IN ('purchase_receipt','sale_dispatch')),
    quantity numeric(20,3) NOT NULL CHECK (quantity>0),
    actor_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    PRIMARY KEY (tenant_id,id),
    FOREIGN KEY (tenant_id,order_id,line_no)
        REFERENCES one_commercial_order_lines(tenant_id,order_id,line_no),
    FOREIGN KEY (tenant_id,warehouse_id,product_id)
        REFERENCES one_stock_balances(tenant_id,warehouse_id,product_id)
);
CREATE INDEX one_commercial_orders_by_partner
    ON one_commercial_orders(tenant_id,partner_id);
CREATE INDEX one_stock_events_by_order
    ON one_stock_events(tenant_id,order_id,line_no,created_at);

-- Preserve tenant defenses for every new domain table, including physical
-- balances and the immutable movement journal.
ALTER TABLE one_products ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_products FORCE ROW LEVEL SECURITY;
CREATE POLICY one_products_tenant ON one_products
    USING (tenant_id::text=current_setting('one.tenant_id',true))
    WITH CHECK (tenant_id::text=current_setting('one.tenant_id',true));
ALTER TABLE one_warehouses ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_warehouses FORCE ROW LEVEL SECURITY;
CREATE POLICY one_warehouses_tenant ON one_warehouses
    USING (tenant_id::text=current_setting('one.tenant_id',true))
    WITH CHECK (tenant_id::text=current_setting('one.tenant_id',true));
ALTER TABLE one_stock_balances ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_stock_balances FORCE ROW LEVEL SECURITY;
CREATE POLICY one_stock_balances_tenant ON one_stock_balances
    USING (tenant_id::text=current_setting('one.tenant_id',true))
    WITH CHECK (tenant_id::text=current_setting('one.tenant_id',true));
ALTER TABLE one_commercial_orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_commercial_orders FORCE ROW LEVEL SECURITY;
CREATE POLICY one_commercial_orders_tenant ON one_commercial_orders
    USING (tenant_id::text=current_setting('one.tenant_id',true))
    WITH CHECK (tenant_id::text=current_setting('one.tenant_id',true));
ALTER TABLE one_commercial_order_lines ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_commercial_order_lines FORCE ROW LEVEL SECURITY;
CREATE POLICY one_commercial_lines_tenant ON one_commercial_order_lines
    USING (tenant_id::text=current_setting('one.tenant_id',true))
    WITH CHECK (tenant_id::text=current_setting('one.tenant_id',true));
ALTER TABLE one_stock_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_stock_events FORCE ROW LEVEL SECURITY;
CREATE POLICY one_stock_events_tenant ON one_stock_events
    USING (tenant_id::text=current_setting('one.tenant_id',true))
    WITH CHECK (tenant_id::text=current_setting('one.tenant_id',true));

-- Append-only stock events. Inventory changes must have a durable event.
CREATE FUNCTION one_stock_event_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Stock events cannot be changed or deleted';
END $$;
CREATE TRIGGER one_stock_events_immutable BEFORE UPDATE OR DELETE
    ON one_stock_events FOR EACH ROW EXECUTE FUNCTION one_stock_event_immutable();

CREATE FUNCTION one_stock_balance_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' OR
       current_setting('one.commerce_context',true) IS DISTINCT FROM 'yes' THEN
        RAISE EXCEPTION 'Stock balances can only change through trusted movement';
    END IF;
    IF TG_OP='UPDATE' AND ROW(NEW.tenant_id,NEW.product_id,NEW.warehouse_id)
         IS DISTINCT FROM ROW(OLD.tenant_id,OLD.product_id,OLD.warehouse_id) THEN
        RAISE EXCEPTION 'Stock identity cannot change';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER one_stock_balance_guard BEFORE INSERT OR UPDATE OR DELETE
    ON one_stock_balances FOR EACH ROW EXECUTE FUNCTION one_stock_balance_guard();

CREATE FUNCTION one_commercial_order_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Commercial orders cannot be deleted'; END IF;
    IF TG_OP='INSERT' THEN
        IF NEW.status<>'draft' OR NEW.confirmed_at IS NOT NULL OR
           NEW.confirmed_by IS NOT NULL THEN
            RAISE EXCEPTION 'Commercial orders must start as drafts';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.status<>'draft' OR NEW.status<>'confirmed' OR
       current_setting('one.commerce_context',true) IS DISTINCT FROM 'yes' OR
       ROW(NEW.tenant_id,NEW.id,NEW.kind,NEW.number,NEW.partner_id,NEW.currency)
       IS DISTINCT FROM
       ROW(OLD.tenant_id,OLD.id,OLD.kind,OLD.number,OLD.partner_id,OLD.currency) THEN
        RAISE EXCEPTION 'Only trusted confirmation of an unchanged draft is allowed';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER one_commercial_order_guard BEFORE INSERT OR UPDATE OR DELETE
    ON one_commercial_orders FOR EACH ROW EXECUTE FUNCTION one_commercial_order_guard();

CREATE FUNCTION one_commercial_line_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE state text;
BEGIN
    SELECT status INTO state FROM one_commercial_orders
      WHERE tenant_id=CASE WHEN TG_OP='DELETE' THEN OLD.tenant_id ELSE NEW.tenant_id END
        AND id=CASE WHEN TG_OP='DELETE' THEN OLD.order_id ELSE NEW.order_id END
      FOR UPDATE;
    IF state IS NULL THEN RAISE EXCEPTION 'Parent commercial order required'; END IF;
    IF state='draft' THEN
        IF TG_OP<>'INSERT' AND OLD.fulfilled_qty<>0 THEN
            RAISE EXCEPTION 'Fulfilled line cannot be modified';
        END IF;
        IF TG_OP<>'DELETE' AND NEW.fulfilled_qty<>0 THEN
            RAISE EXCEPTION 'Draft lines must start unfulfilled';
        END IF;
        IF TG_OP='DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
    END IF;
    IF TG_OP<>'UPDATE' OR
       current_setting('one.commerce_context',true) IS DISTINCT FROM 'yes' OR
       NEW.fulfilled_qty<=OLD.fulfilled_qty OR
       ROW(NEW.tenant_id,NEW.order_id,NEW.line_no,NEW.product_id,
           NEW.warehouse_id,NEW.ordered_qty,NEW.unit_price)
       IS DISTINCT FROM
       ROW(OLD.tenant_id,OLD.order_id,OLD.line_no,OLD.product_id,
           OLD.warehouse_id,OLD.ordered_qty,OLD.unit_price) THEN
        RAISE EXCEPTION 'Only trusted monotonic fulfillment may change confirmed lines';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER one_commercial_line_guard BEFORE INSERT OR UPDATE OR DELETE
    ON one_commercial_order_lines FOR EACH ROW EXECUTE FUNCTION one_commercial_line_guard();

-- Intentional narrow first milestone: company_admin only. Future versioned
-- stock-specific role grants require new SQL membership policy and tests.
CREATE FUNCTION one_confirm_commercial_order(
    p_tenant uuid, p_order uuid, p_actor uuid
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE o one_commercial_orders%ROWTYPE;
BEGIN
    IF p_tenant IS NULL OR p_order IS NULL OR p_actor IS NULL OR
       current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
       current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
       NOT EXISTS (
           SELECT 1 FROM one_memberships
           WHERE tenant_id=p_tenant AND actor_id=p_actor AND active
             AND role='company_admin'
       ) THEN
        RAISE EXCEPTION 'Authorized company administrator required';
    END IF;
    SELECT * INTO o FROM one_commercial_orders
      WHERE tenant_id=p_tenant AND id=p_order FOR UPDATE;
    IF NOT FOUND OR o.status<>'draft' THEN
        RAISE EXCEPTION 'Draft commercial order required';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM one_commercial_order_lines
        WHERE tenant_id=p_tenant AND order_id=p_order
    ) OR EXISTS (
        SELECT 1 FROM one_commercial_order_lines l
        JOIN one_products p ON p.tenant_id=l.tenant_id AND p.id=l.product_id
        JOIN one_warehouses w ON w.tenant_id=l.tenant_id AND w.id=l.warehouse_id
        CROSS JOIN one_currency_precision c
        WHERE l.tenant_id=p_tenant AND l.order_id=p_order
          AND c.code=o.currency
          AND (NOT p.active OR NOT w.active OR
              l.unit_price<>round(l.unit_price,c.minor_units))
    ) THEN
        RAISE EXCEPTION 'Invalid product, warehouse or price precision';
    END IF;
    PERFORM set_config('one.commerce_context','yes',true);
    UPDATE one_commercial_orders
       SET status='confirmed',confirmed_at=transaction_timestamp(),
           confirmed_by=p_actor
     WHERE tenant_id=p_tenant AND id=p_order;
    RETURN p_order;
END $$;

CREATE FUNCTION one_fulfill_commercial_line(
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
        RAISE EXCEPTION 'Authorized positive stock movement required';
    END IF;
    SELECT * INTO o FROM one_commercial_orders
      WHERE tenant_id=p_tenant AND id=p_order FOR UPDATE;
    IF NOT FOUND OR o.status<>'confirmed' THEN
        RAISE EXCEPTION 'Confirmed commercial order required';
    END IF;
    SELECT * INTO l FROM one_commercial_order_lines
      WHERE tenant_id=p_tenant AND order_id=p_order AND line_no=p_line_no
      FOR UPDATE;
    IF NOT FOUND OR l.ordered_qty-l.fulfilled_qty<p_quantity THEN
        RAISE EXCEPTION 'Stock movement exceeds open commercial order quantity';
    END IF;
    IF EXISTS (
        SELECT 1 FROM one_stock_events
        WHERE tenant_id=p_tenant AND id=p_event
    ) THEN
        RAISE EXCEPTION 'Stock movement event already recorded';
    END IF;
    IF NOT EXISTS(
        SELECT 1 FROM one_products WHERE tenant_id=p_tenant AND id=l.product_id
          AND active
    ) OR NOT EXISTS(
        SELECT 1 FROM one_warehouses WHERE tenant_id=p_tenant
          AND id=l.warehouse_id AND active
    ) THEN
        RAISE EXCEPTION 'Inactive catalog entity cannot move stock';
    END IF;

    PERFORM set_config('one.commerce_context','yes',true);
    IF o.kind='purchase' THEN
        INSERT INTO one_stock_balances(tenant_id,warehouse_id,product_id)
            VALUES(p_tenant,l.warehouse_id,l.product_id)
            ON CONFLICT (tenant_id,warehouse_id,product_id) DO NOTHING;
        UPDATE one_stock_balances
           SET on_hand=on_hand+p_quantity
         WHERE tenant_id=p_tenant AND warehouse_id=l.warehouse_id
           AND product_id=l.product_id;
    ELSE
        UPDATE one_stock_balances
           SET on_hand=on_hand-p_quantity
         WHERE tenant_id=p_tenant AND warehouse_id=l.warehouse_id
           AND product_id=l.product_id
           AND on_hand-reserved>=p_quantity;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'Insufficient unreserved stock';
        END IF;
    END IF;

    UPDATE one_commercial_order_lines SET fulfilled_qty=fulfilled_qty+p_quantity
     WHERE tenant_id=p_tenant AND order_id=p_order AND line_no=p_line_no;
    INSERT INTO one_stock_events(
        tenant_id,id,order_id,line_no,warehouse_id,product_id,
        movement_kind,quantity,actor_id
    ) VALUES(
        p_tenant,p_event,p_order,p_line_no,l.warehouse_id,l.product_id,
        CASE WHEN o.kind='sale' THEN 'sale_dispatch' ELSE 'purchase_receipt' END,
        p_quantity,p_actor
    );
    RETURN p_event;
END $$;

-- Important: revoke implicit function execution from PUBLIC. Real app grants
-- need a separate reviewed backend-only role, never a browser/tenant login.
REVOKE ALL ON FUNCTION one_confirm_commercial_order(uuid,uuid,uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION one_fulfill_commercial_line(uuid,uuid,integer,numeric,uuid,uuid) FROM PUBLIC;
COMMIT;
