-- ONE ERP Next / 013: quarantine is NOT a sellable stock location.
-- Postgres 14 prototype only. This prevents quoting/fulfilling customer sales
-- from a warehouse reserved for physical QC returns, even when on_hand > 0.
BEGIN;
CREATE FUNCTION one_sale_line_sellable_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE order_kind text; warehouse_kind text; warehouse_active boolean;
BEGIN
  SELECT kind INTO order_kind FROM one_commercial_orders
    WHERE tenant_id=NEW.tenant_id AND id=NEW.order_id;
  IF order_kind='sale' THEN
    SELECT warehouse_type,active INTO warehouse_kind,warehouse_active
      FROM one_warehouses
      WHERE tenant_id=NEW.tenant_id AND id=NEW.warehouse_id;
    IF warehouse_kind IS DISTINCT FROM 'sellable'
       OR warehouse_active IS DISTINCT FROM true THEN
      RAISE EXCEPTION 'Sales order line may use only an active sellable warehouse';
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER one_sale_line_sellable_guard
  BEFORE INSERT OR UPDATE ON one_commercial_order_lines
  FOR EACH ROW EXECUTE FUNCTION one_sale_line_sellable_guard();

-- Warehouses are classified once. Changing their kind after stocking or
-- referencing orders would silently turn quarantined goods into sellable.
CREATE FUNCTION one_warehouse_type_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER AS $$
BEGIN
  IF NEW.warehouse_type IS DISTINCT FROM OLD.warehouse_type THEN
    RAISE EXCEPTION 'Warehouse stock classification cannot be changed in place';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER one_warehouse_type_guard
  BEFORE UPDATE OF warehouse_type ON one_warehouses
  FOR EACH ROW EXECUTE FUNCTION one_warehouse_type_guard();

-- Refuse to install on datasets with legacy sales pointing at quarantine.
DO $$
BEGIN
 IF EXISTS (
   SELECT 1 FROM one_commercial_order_lines l
   JOIN one_commercial_orders o
     ON o.tenant_id=l.tenant_id AND o.id=l.order_id
   JOIN one_warehouses w
     ON w.tenant_id=l.tenant_id AND w.id=l.warehouse_id
   WHERE o.kind='sale' AND (w.warehouse_type<>'sellable' OR NOT w.active)
 ) THEN
   RAISE EXCEPTION 'Cannot enforce saleable warehouse invariant: review existing sales';
 END IF;
END $$;
COMMIT;
