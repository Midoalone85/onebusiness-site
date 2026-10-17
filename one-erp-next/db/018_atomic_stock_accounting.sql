-- ONE ERP Next / 018: physical movement and inventory journal in ONE
-- PostgreSQL statement/transaction. No external HTTP/API authorization here.
BEGIN;

CREATE FUNCTION one_receive_and_value_goods(
 p_tenant uuid,p_order uuid,p_line integer,p_qty numeric,
 p_stock_event uuid,p_cost_journal uuid,p_period uuid,
 p_inventory_account text,p_grni_account text,p_actor uuid
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE kind text;
BEGIN
 SELECT o.kind INTO kind FROM one_commercial_orders o
 WHERE o.tenant_id=p_tenant AND o.id=p_order;
 IF kind IS DISTINCT FROM 'purchase' THEN
  RAISE EXCEPTION 'Purchase receipt with original purchase order required';
 END IF;
 -- Any failure in either operation rolls the entire enclosing SQL call back.
 PERFORM one_fulfill_commercial_line(p_tenant,p_order,p_line,p_qty,
                                    p_stock_event,p_actor);
 PERFORM one_value_physical_stock_event(
   p_tenant,p_stock_event,p_period,p_inventory_account,
   p_grni_account,p_cost_journal,p_actor);
 RETURN p_stock_event;
END $$;

CREATE FUNCTION one_ship_and_value_goods(
 p_tenant uuid,p_order uuid,p_line integer,p_qty numeric,
 p_stock_event uuid,p_cost_journal uuid,p_period uuid,
 p_inventory_account text,p_cogs_account text,p_actor uuid
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE kind text;
BEGIN
 SELECT o.kind INTO kind FROM one_commercial_orders o
 WHERE o.tenant_id=p_tenant AND o.id=p_order;
 IF kind IS DISTINCT FROM 'sale' THEN
  RAISE EXCEPTION 'Confirmed customer sale required for valued dispatch';
 END IF;
 PERFORM one_fulfill_commercial_line(p_tenant,p_order,p_line,p_qty,
                                    p_stock_event,p_actor);
 PERFORM one_value_physical_stock_event(
  p_tenant,p_stock_event,p_period,p_inventory_account,
  p_cogs_account,p_cost_journal,p_actor);
 RETURN p_stock_event;
END $$;

CREATE FUNCTION one_dispatch_reserved_and_value_goods(
 p_tenant uuid,p_order uuid,p_line integer,p_qty numeric,
 p_stock_event uuid,p_cost_journal uuid,p_period uuid,
 p_inventory_account text,p_cogs_account text,p_actor uuid
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $$
BEGIN
 PERFORM one_dispatch_reserved_sale_line(p_tenant,p_order,p_line,p_qty,
                                         p_stock_event,p_actor);
 PERFORM one_value_physical_stock_event(
  p_tenant,p_stock_event,p_period,p_inventory_account,
  p_cogs_account,p_cost_journal,p_actor);
 RETURN p_stock_event;
END $$;

-- Delivery already picked/packed: commit stock dispatch, COGS and
-- delivery stage/audit in the same transaction or roll back all of them.
CREATE FUNCTION one_dispatch_delivery_and_value(
 p_tenant uuid,p_delivery_job uuid,p_stock_event uuid,p_cost_journal uuid,
 p_period uuid,p_inventory_account text,p_cogs_account text,p_actor uuid
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $$
BEGIN
 PERFORM one_advance_delivery(
  p_tenant,p_delivery_job,'dispatched',p_actor,p_stock_event,NULL);
 PERFORM one_value_physical_stock_event(
  p_tenant,p_stock_event,p_period,p_inventory_account,
  p_cogs_account,p_cost_journal,p_actor);
 RETURN p_delivery_job;
END $$;

REVOKE ALL ON FUNCTION one_receive_and_value_goods(
 uuid,uuid,integer,numeric,uuid,uuid,uuid,text,text,uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION one_ship_and_value_goods(
 uuid,uuid,integer,numeric,uuid,uuid,uuid,text,text,uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION one_dispatch_reserved_and_value_goods(
 uuid,uuid,integer,numeric,uuid,uuid,uuid,text,text,uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION one_dispatch_delivery_and_value(
 uuid,uuid,uuid,uuid,uuid,text,text,uuid) FROM PUBLIC;
COMMIT;
