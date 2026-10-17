-- ONE ERP Next / 028 | NRV allowance release for reserved and packed deliveries.
-- PostgreSQL 14 CI ONLY. Does not imply real-world goods delivery or POD.
-- Uses the original issue/gross COGS and a second allowance-to-COGS
-- journal in one transaction. No standalone posted release is allowed.
BEGIN;
CREATE FUNCTION one_dispatch_reserved_or_delivery_with_nrv(
  p_tenant uuid,p_order uuid,p_line integer,p_delivery uuid,p_qty numeric,
  p_stock_event uuid,p_gross_journal uuid,p_release_journal uuid,
  p_period uuid,p_inventory text,p_cogs text,p_allowance text,p_actor uuid
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE
 ord one_commercial_orders%ROWTYPE;
 d one_delivery_jobs%ROWTYPE;
 l one_commercial_order_lines%ROWTYPE;
 bal one_stock_balances%ROWTYPE;
 v one_stock_value_positions%ROWTYPE;
 impairment one_inventory_nrv_positions%ROWTYPE;
 valued one_stock_value_events%ROWTYPE;
 qty_left numeric(20,3);
 value_left numeric(20,3);
 allowance_left numeric(20,3);
 gross_cost numeric(20,3);
 allocated numeric(20,3);
 digits integer;
BEGIN
 IF p_tenant IS NULL OR p_order IS NULL OR p_line IS NULL OR p_line<=0 OR
    p_qty IS NULL OR p_qty<=0 OR p_qty<>round(p_qty,3)
    OR p_qty::text='NaN' OR p_stock_event IS NULL OR
    p_gross_journal IS NULL OR p_release_journal IS NULL OR
    p_period IS NULL OR p_actor IS NULL OR
    p_stock_event IN (p_gross_journal,p_release_journal) OR
    p_gross_journal=p_release_journal OR
    p_inventory IS NULL OR p_cogs IS NULL OR p_allowance IS NULL OR
    p_inventory IN (p_cogs,p_allowance) OR p_cogs=p_allowance OR
    current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
    current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
    NOT EXISTS (SELECT 1 FROM one_memberships m WHERE m.tenant_id=p_tenant
      AND m.actor_id=p_actor AND m.active AND m.role='company_admin') THEN
   RAISE EXCEPTION 'Authenticated, non-repeating allowance dispatch required';
 END IF;

 SELECT * INTO ord FROM one_commercial_orders
   WHERE tenant_id=p_tenant AND id=p_order FOR UPDATE;
 IF NOT FOUND OR ord.kind<>'sale' OR ord.status<>'confirmed' THEN
   RAISE EXCEPTION 'Confirmed customer sales order required';
 END IF;
 IF p_delivery IS NOT NULL THEN
  SELECT * INTO d FROM one_delivery_jobs WHERE tenant_id=p_tenant
    AND id=p_delivery FOR UPDATE;
  IF NOT FOUND OR d.order_id<>p_order OR d.line_no<>p_line OR
     d.quantity<>p_qty OR d.status<>'packed' THEN
    RAISE EXCEPTION 'Only packed matching delivery can be dispatched';
  END IF;
 ELSIF EXISTS (
   SELECT 1 FROM one_delivery_jobs j
   WHERE j.tenant_id=p_tenant AND j.order_id=p_order
     AND j.line_no=p_line AND j.status IN ('draft','picked','packed')
 ) THEN
  RAISE EXCEPTION 'Reserved stock assigned to open delivery job';
 END IF;

 SELECT * INTO l FROM one_commercial_order_lines
   WHERE tenant_id=p_tenant AND order_id=p_order AND line_no=p_line FOR UPDATE;
 IF NOT FOUND OR l.reserved_qty<p_qty OR
    l.ordered_qty-l.fulfilled_qty<p_qty THEN
   RAISE EXCEPTION 'Quantity exceeds outstanding reservation or sale';
 END IF;
 SELECT * INTO bal FROM one_stock_balances
   WHERE tenant_id=p_tenant AND warehouse_id=l.warehouse_id
     AND product_id=l.product_id FOR UPDATE;
 SELECT * INTO v FROM one_stock_value_positions
   WHERE tenant_id=p_tenant AND warehouse_id=l.warehouse_id
     AND product_id=l.product_id FOR UPDATE;
 SELECT * INTO impairment FROM one_inventory_nrv_positions
   WHERE tenant_id=p_tenant AND warehouse_id=l.warehouse_id
     AND product_id=l.product_id FOR UPDATE;
 IF bal.tenant_id IS NULL OR v.tenant_id IS NULL OR
    impairment.tenant_id IS NULL OR impairment.allowance<=0 OR
    impairment.cost_qty<>v.qty OR impairment.gross_cost<>v.book_value OR
    v.qty<>bal.on_hand OR bal.reserved<p_qty OR
    v.qty<p_qty OR v.currency<>ord.currency OR
    impairment.currency<>v.currency THEN
   RAISE EXCEPTION 'Active IAS 2 allowance needs locked, reconciled, reserved stock';
 END IF;
 IF p_allowance<>impairment.allowance_account_id OR
    NOT EXISTS(SELECT 1 FROM one_accounts WHERE tenant_id=p_tenant
       AND id=p_allowance AND kind='asset' AND active AND posting_allowed) OR
    NOT EXISTS(SELECT 1 FROM one_accounts WHERE tenant_id=p_tenant
       AND id=p_inventory AND kind='asset' AND active AND posting_allowed) OR
    NOT EXISTS(SELECT 1 FROM one_accounts WHERE tenant_id=p_tenant
       AND id=p_cogs AND kind='expense' AND active AND posting_allowed) THEN
   RAISE EXCEPTION 'Correct inventory/COGS/contra-inventory chart mapping required';
 END IF;
 SELECT minor_units INTO digits FROM one_currency_precision
   WHERE code=v.currency;
 IF digits IS NULL THEN RAISE EXCEPTION 'Unsupported currency precision'; END IF;
 qty_left=v.qty-p_qty;
 gross_cost=CASE WHEN qty_left=0 THEN v.book_value ELSE
   round(v.book_value*p_qty/v.qty,digits) END;
 allocated=CASE WHEN qty_left=0 THEN impairment.allowance ELSE
   round(impairment.allowance*p_qty/v.qty,digits) END;
 value_left=v.book_value-gross_cost;
 allowance_left=impairment.allowance-allocated;
 IF gross_cost<=0 OR allocated<=0 OR allocated>gross_cost OR
    allowance_left<0 OR allowance_left>value_left OR
    (qty_left=0 AND (value_left<>0 OR allowance_left<>0)) OR
    (qty_left>0 AND value_left<=0) THEN
   RAISE EXCEPTION 'Unrepresentable NRV allowance allocation';
 END IF;

 -- Permits only the exact scoped physical sale event. The trigger's
 -- previous active-allowance freeze stays enforced for other movement types.
 PERFORM set_config('one.nrv_dispatch_event',p_stock_event::text,true);
 IF p_delivery IS NULL THEN
  PERFORM one_dispatch_reserved_and_value_goods(
    p_tenant,p_order,p_line,p_qty,p_stock_event,p_gross_journal,
    p_period,p_inventory,p_cogs,p_actor);
 ELSE
  PERFORM one_dispatch_delivery_and_value(
    p_tenant,p_delivery,p_stock_event,p_gross_journal,p_period,
    p_inventory,p_cogs,p_actor);
 END IF;
 PERFORM set_config('one.nrv_dispatch_event','',true);
 SELECT * INTO valued FROM one_stock_value_events
    WHERE tenant_id=p_tenant AND stock_event_id=p_stock_event;
 IF NOT FOUND OR valued.movement_kind<>'sale_dispatch' OR
    valued.journal_id<>p_gross_journal OR valued.qty<>p_qty OR
    valued.amount<>gross_cost OR valued.position_qty_after<>qty_left OR
    valued.position_value_after<>value_left THEN
   RAISE EXCEPTION 'Original gross-cost journal differs from reserved sale';
 END IF;

 PERFORM set_config('one.nrv_context','yes',true);
 UPDATE one_inventory_nrv_positions SET
    cost_qty=qty_left,gross_cost=value_left,allowance=allowance_left,
    revision=revision+1,updated_at=transaction_timestamp()
 WHERE tenant_id=p_tenant AND warehouse_id=l.warehouse_id
   AND product_id=l.product_id;
 PERFORM set_config('one.nrv_context','',true);
 INSERT INTO one_journal_entries
   (tenant_id,id,period_id,reference,idempotency_key,occurred_on,currency)
 VALUES(p_tenant,p_release_journal,p_period,
    'nrv-reserved-sale-release:'||p_stock_event::text,
    'nrv-reserved-sale-release:'||p_stock_event::text,
    valued.recorded_at::date,v.currency);
 INSERT INTO one_journal_lines
   (tenant_id,entry_id,line_number,account_id,debit,credit)
 VALUES(p_tenant,p_release_journal,1,p_allowance,allocated,0),
       (p_tenant,p_release_journal,2,p_cogs,0,allocated);
 PERFORM one_post_journal(p_tenant,p_release_journal,p_actor);
 INSERT INTO one_inventory_nrv_sale_releases(
  tenant_id,stock_event_id,gross_cogs_journal_id,
  allowance_release_journal_id,warehouse_id,product_id,quantity,
  gross_cogs,released_allowance,net_cogs,allowance_after,actor_id)
 VALUES(p_tenant,p_stock_event,p_gross_journal,p_release_journal,
  l.warehouse_id,l.product_id,p_qty,gross_cost,allocated,
  gross_cost-allocated,allowance_left,p_actor);
 RETURN coalesce(p_delivery,p_stock_event);
END $$;
REVOKE ALL ON FUNCTION one_dispatch_reserved_or_delivery_with_nrv(
 uuid,uuid,integer,uuid,numeric,uuid,uuid,uuid,uuid,text,text,text,uuid
) FROM PUBLIC;
COMMIT;
