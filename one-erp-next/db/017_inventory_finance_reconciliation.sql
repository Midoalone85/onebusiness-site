-- ONE ERP Next 017 | Source-backed inventory / GRNI reconciliation read model.
-- PostgreSQL 14 CI only. No live ONE PULSE dashboard or production API.
BEGIN;
CREATE FUNCTION one_inventory_finance_reconciliation(
 p_tenant uuid,p_warehouse uuid,p_product uuid,p_actor uuid
) RETURNS TABLE(
 physical_qty numeric, reserved_qty numeric, available_qty numeric,
 valued_qty numeric, valued_amount numeric, unvalued_events bigint,
 grni_received_value numeric, grni_billed_value numeric,
 grni_outstanding_value numeric, reconciliation_ok boolean
) LANGUAGE plpgsql STABLE SECURITY INVOKER AS $$
BEGIN
 IF p_tenant IS NULL OR p_warehouse IS NULL OR p_product IS NULL
    OR p_actor IS NULL OR
    current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
    current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
    NOT EXISTS(SELECT 1 FROM one_memberships m
      WHERE m.tenant_id=p_tenant AND m.actor_id=p_actor AND m.active
        AND m.role IN ('company_admin','accountant','viewer')) THEN
  RAISE EXCEPTION 'Company inventory finance report authorization required';
 END IF;
 RETURN QUERY
 SELECT
  b.on_hand::numeric,b.reserved::numeric,(b.on_hand-b.reserved)::numeric,
  coalesce(v.qty,0)::numeric,coalesce(v.book_value,0)::numeric,
  missed.unvalued_count,
  received.total_value,
  billed.total_value,
  (received.total_value-billed.total_value)::numeric,
  (missed.unvalued_count=0
   AND b.on_hand=coalesce(v.qty,0)
   AND received.total_value>=billed.total_value)
 FROM one_stock_balances b
 LEFT JOIN one_stock_value_positions v
   ON v.tenant_id=b.tenant_id AND v.warehouse_id=b.warehouse_id
    AND v.product_id=b.product_id
 CROSS JOIN LATERAL(
  SELECT count(*)::bigint AS unvalued_count
  FROM one_stock_events e
  WHERE e.tenant_id=b.tenant_id AND e.warehouse_id=b.warehouse_id
    AND e.product_id=b.product_id
    AND NOT EXISTS (
     SELECT 1 FROM one_stock_value_events x
     WHERE x.tenant_id=e.tenant_id AND x.stock_event_id=e.id
    )
 ) missed
 CROSS JOIN LATERAL(
  SELECT coalesce(sum(vs.amount),0)::numeric AS total_value
  FROM one_stock_value_events vs
  JOIN one_stock_events e ON e.tenant_id=vs.tenant_id
    AND e.id=vs.stock_event_id
  WHERE e.tenant_id=b.tenant_id AND e.warehouse_id=b.warehouse_id
    AND e.product_id=b.product_id AND e.movement_kind='purchase_receipt'
 ) received
 CROSS JOIN LATERAL(
  SELECT coalesce(sum(al.billed_qty*il.unit_price),0)::numeric AS total_value
  FROM one_goods_bill_allocations al
  JOIN one_invoice_lines il ON il.tenant_id=al.tenant_id
    AND il.doc_id=al.bill_id AND il.line_number=al.bill_line_no
  JOIN one_commercial_order_lines po
    ON po.tenant_id=al.tenant_id AND po.order_id=al.order_id
    AND po.line_no=al.order_line_no
  WHERE al.tenant_id=b.tenant_id AND po.warehouse_id=b.warehouse_id
    AND po.product_id=b.product_id
 ) billed
 WHERE b.tenant_id=p_tenant AND b.warehouse_id=p_warehouse
   AND b.product_id=p_product;
END $$;
REVOKE ALL ON FUNCTION
 one_inventory_finance_reconciliation(uuid,uuid,uuid,uuid) FROM PUBLIC;
COMMIT;
