-- ONE ERP Next 021: Accounted QA moves and cost journals in existing read views.
-- Creates no financial documents and no outbound network operations.
BEGIN;
CREATE OR REPLACE FUNCTION one_return_case_timeline(
 p_tenant uuid, p_return uuid, p_actor uuid
) RETURNS TABLE(
 stream_position bigint, event_type text, source_id uuid,
 occurred_at timestamptz
) LANGUAGE plpgsql STABLE SECURITY INVOKER AS $$
BEGIN
 IF p_tenant IS NULL OR p_return IS NULL OR p_actor IS NULL OR
    current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
    current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
    NOT EXISTS(SELECT 1 FROM one_memberships m
       WHERE m.tenant_id=p_tenant AND m.actor_id=p_actor AND m.active
         AND m.role IN ('company_admin','accountant','viewer')) THEN
   RAISE EXCEPTION 'Return timeline requires active company membership';
 END IF;
 -- Avoid enumeration via a return ID belonging to another tenant.
 IF NOT EXISTS(SELECT 1 FROM one_return_jobs j
               WHERE j.tenant_id=p_tenant AND j.id=p_return) THEN
   RAISE EXCEPTION 'Return case not accessible';
 END IF;

 RETURN QUERY
 SELECT o.stream_position,o.event_type,o.source_id,o.created_at
 FROM one_operation_outbox o
 JOIN one_return_jobs j ON j.tenant_id=p_tenant AND j.id=p_return
 WHERE o.tenant_id=p_tenant
   AND (
      (o.source_id=j.id AND o.event_type LIKE 'returns.%')
      OR (o.source_id=j.source_event_id
          AND o.event_type IN ('stock.sale_dispatched','stock.purchase_received'))
      OR (o.source_id=j.processed_event_id
          AND o.event_type IN ('stock.customer_returned','stock.vendor_returned'))
      OR EXISTS (
         SELECT 1 FROM one_return_dispositions d
         WHERE d.tenant_id=j.tenant_id AND d.return_job_id=j.id
           AND (
             (o.source_id=d.id
                AND o.event_type IN ('returns.restocked','returns.disposed'))
             OR (o.source_id=d.stock_event_out_id AND
                 o.event_type IN ('stock.return_restock_out','stock.return_disposal'))
             OR (o.source_id=d.stock_event_in_id AND
                 o.event_type='stock.return_restock_in')
           )
      )
      OR EXISTS(
        SELECT 1 FROM one_customer_return_cost_links rcl
        JOIN one_stock_value_events cost
          ON cost.tenant_id=rcl.tenant_id
         AND cost.stock_event_id=rcl.return_stock_event_id
        WHERE rcl.tenant_id=j.tenant_id AND rcl.return_job_id=j.id
         AND o.source_id=cost.journal_id
         AND o.event_type='finance.journal_posted'
      )
      OR EXISTS(
        SELECT 1 FROM one_return_disposition_costs dc
        WHERE dc.tenant_id=j.tenant_id AND dc.return_job_id=j.id
         AND dc.journal_id=o.source_id
         AND o.event_type='finance.journal_posted'
      )
      OR EXISTS(
         SELECT 1 FROM one_customer_return_credits cr
         WHERE cr.tenant_id=j.tenant_id AND cr.return_job_id=j.id
           AND cr.credit_invoice_id=o.source_id
           AND o.event_type IN (
              'finance.customer_return_credited','finance.invoice_issued',
              'finance.journal_posted'
           )
      )
   )
 ORDER BY o.stream_position;
END $$;

CREATE OR REPLACE FUNCTION one_inventory_finance_reconciliation(
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
    AND NOT EXISTS (
     SELECT 1 FROM one_return_disposition_costs c
     JOIN one_return_dispositions d
       ON d.tenant_id=c.tenant_id AND d.id=c.disposition_id
     WHERE c.tenant_id=e.tenant_id
       AND (d.stock_event_out_id=e.id OR d.stock_event_in_id=e.id)
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
COMMIT;
