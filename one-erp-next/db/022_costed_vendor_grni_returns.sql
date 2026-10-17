-- ONE ERP Next / 022 | Unbilled supplier-return source-cost reversal.
-- PostgreSQL 14 isolated CI only. No approved vendor credit / VAT path.
BEGIN;
ALTER TABLE one_stock_value_events
 DROP CONSTRAINT one_stock_value_events_movement_kind_check;
ALTER TABLE one_stock_value_events
 ADD CONSTRAINT one_stock_value_events_movement_kind_check
 CHECK(movement_kind IN
  ('purchase_receipt','sale_dispatch','customer_return','vendor_return'));

CREATE TABLE one_vendor_return_cost_links (
 tenant_id uuid NOT NULL,
 return_job_id uuid NOT NULL,
 source_receipt_id uuid NOT NULL,
 stock_event_id uuid NOT NULL,
 qty numeric(20,3) NOT NULL CHECK(qty>0),
 amount numeric(20,3) NOT NULL CHECK(amount>0),
 actor_id uuid NOT NULL,
 created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 PRIMARY KEY(tenant_id,return_job_id),
 UNIQUE(tenant_id,stock_event_id),
 FOREIGN KEY(tenant_id,return_job_id)
  REFERENCES one_return_jobs(tenant_id,id),
 FOREIGN KEY(tenant_id,source_receipt_id)
  REFERENCES one_stock_events(tenant_id,id),
 FOREIGN KEY(tenant_id,stock_event_id)
  REFERENCES one_stock_value_events(tenant_id,stock_event_id)
);
ALTER TABLE one_vendor_return_cost_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_vendor_return_cost_links FORCE ROW LEVEL SECURITY;
CREATE POLICY one_vendor_return_cost_scope ON one_vendor_return_cost_links
 USING(tenant_id::text=current_setting('one.tenant_id',true))
 WITH CHECK(tenant_id::text=current_setting('one.tenant_id',true));
CREATE TRIGGER one_vendor_return_cost_immutable BEFORE UPDATE OR DELETE
 ON one_vendor_return_cost_links FOR EACH ROW EXECUTE FUNCTION one_protect_audit();

CREATE FUNCTION one_complete_unbilled_vendor_return(
 p_tenant uuid,p_return uuid,p_stock_event uuid,p_journal uuid,p_period uuid,
 p_inventory text,p_grni text,p_actor uuid
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE
 source_order uuid;
 returned one_return_jobs%ROWTYPE;
 source one_stock_events%ROWTYPE;
 valued one_stock_value_events%ROWTYPE;
 pos one_stock_value_positions%ROWTYPE;
 physical one_stock_balances%ROWTYPE;
 source_qty numeric(24,3);
 already_billed numeric(24,3);
 physically_returned numeric(24,3);
 money numeric(20,3);
 scale integer;
 new_qty numeric(20,3);
 new_value numeric(20,3);
BEGIN
 IF p_tenant IS NULL OR p_return IS NULL OR p_stock_event IS NULL
    OR p_journal IS NULL OR p_period IS NULL OR p_actor IS NULL
    OR p_inventory IS NULL OR p_grni IS NULL OR p_inventory=p_grni
    OR p_stock_event=p_journal OR
    current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text
    OR current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text
    OR NOT EXISTS(SELECT 1 FROM one_memberships m
      WHERE m.tenant_id=p_tenant AND m.actor_id=p_actor
       AND m.active AND m.role='company_admin') THEN
  RAISE EXCEPTION 'Authorized unbilled supplier return with valuation required';
 END IF;
 SELECT e.order_id INTO source_order
  FROM one_return_jobs job JOIN one_stock_events e
    ON e.tenant_id=job.tenant_id AND e.id=job.source_event_id
  WHERE job.tenant_id=p_tenant AND job.id=p_return;
 IF source_order IS NULL THEN RAISE EXCEPTION 'Return source order unavailable'; END IF;
 PERFORM 1 FROM one_commercial_orders
  WHERE tenant_id=p_tenant AND id=source_order FOR UPDATE;
 SELECT * INTO returned FROM one_return_jobs
  WHERE tenant_id=p_tenant AND id=p_return FOR UPDATE;
 IF NOT FOUND OR returned.return_kind<>'vendor' OR returned.status<>'requested'
    OR returned.quantity<=0 THEN
  RAISE EXCEPTION 'Only new vendor return can be physically accepted';
 END IF;
 SELECT * INTO source FROM one_stock_events
  WHERE tenant_id=p_tenant AND id=returned.source_event_id FOR UPDATE;
 IF NOT FOUND OR source.movement_kind<>'purchase_receipt' THEN
  RAISE EXCEPTION 'Original vendor receipt required';
 END IF;
 SELECT * INTO valued FROM one_stock_value_events
  WHERE tenant_id=p_tenant AND stock_event_id=source.id FOR UPDATE;
 IF NOT FOUND OR valued.movement_kind<>'purchase_receipt'
    OR valued.qty<>source.quantity THEN
  RAISE EXCEPTION 'Original supplier receipt must be fully valued';
 END IF;
 SELECT minor_units INTO scale FROM one_currency_precision
  WHERE code=valued.currency;
 IF scale IS NULL THEN RAISE EXCEPTION 'Source currency precision missing'; END IF;
 -- Quantity is still GRNI (received but NOT billed). Billed vendor goods
 -- must use separate supplier-credit process, not an unbilled GRNI reversal.
 SELECT coalesce(sum(qty),0) INTO source_qty
  FROM one_stock_value_events val JOIN one_stock_events evt
   ON evt.tenant_id=val.tenant_id AND evt.id=val.stock_event_id
  WHERE evt.tenant_id=p_tenant AND evt.order_id=source.order_id
    AND evt.line_no=source.line_no AND evt.movement_kind='purchase_receipt';
 SELECT coalesce(sum(billed_qty),0) INTO already_billed
  FROM one_goods_bill_allocations
  WHERE tenant_id=p_tenant AND order_id=source.order_id
    AND order_line_no=source.line_no;
 SELECT coalesce(sum(quantity),0) INTO physically_returned
  FROM one_stock_events
  WHERE tenant_id=p_tenant AND order_id=source.order_id
    AND line_no=source.line_no AND movement_kind='vendor_return';
 IF source_qty-already_billed-physically_returned<returned.quantity THEN
  RAISE EXCEPTION 'Supplier billed or previously returned source quantity';
 END IF;
 IF NOT EXISTS(SELECT 1 FROM one_accounts WHERE tenant_id=p_tenant
   AND id=p_inventory AND kind='asset' AND active AND posting_allowed)
 OR NOT EXISTS(SELECT 1 FROM one_accounts WHERE tenant_id=p_tenant
   AND id=p_grni AND kind='liability' AND active AND posting_allowed) THEN
  RAISE EXCEPTION 'Active inventory and GRNI source accounts required';
 END IF;
 SELECT * INTO pos FROM one_stock_value_positions
  WHERE tenant_id=p_tenant AND warehouse_id=source.warehouse_id
   AND product_id=source.product_id FOR UPDATE;
 IF NOT FOUND OR pos.qty<returned.quantity OR
    pos.currency<>valued.currency OR pos.qty=0 THEN
  RAISE EXCEPTION 'Insufficient valued supplier-return stock';
 END IF;
 -- Strict first-cut policy: source cost must equal current book-average.
 -- Mixed-cost inventory is blocked until a source-layer/variance ledger.
 money=round(valued.amount*returned.quantity/valued.qty,scale);
 IF money<=0 OR
    round(pos.book_value*returned.quantity/pos.qty,scale)<>money THEN
  RAISE EXCEPTION 'Vendor return requires homogeneous source cost';
 END IF;
 IF pos.qty=returned.quantity THEN money=pos.book_value; END IF;
 new_qty=pos.qty-returned.quantity;
 new_value=pos.book_value-money;
 IF money<=0 OR new_value<0
    OR (new_qty=0 AND new_value<>0)
    OR (new_qty>0 AND new_value<=0) THEN
  RAISE EXCEPTION 'Book inventory cannot retain unsupported value';
 END IF;
 -- Physical, financial position, GRNI journal and audit all commit or none.
 PERFORM one_finish_return(
  p_tenant,p_return,returned.quantity,p_stock_event,p_actor);
 SELECT * INTO physical FROM one_stock_balances
  WHERE tenant_id=p_tenant AND warehouse_id=source.warehouse_id
   AND product_id=source.product_id FOR UPDATE;
 IF NOT FOUND OR physical.on_hand<>new_qty THEN
  RAISE EXCEPTION 'Vendor return physical balance differs from book quantity';
 END IF;
 PERFORM set_config('one.valuation_context','yes',true);
 UPDATE one_stock_value_positions SET qty=new_qty,book_value=new_value
 WHERE tenant_id=p_tenant AND warehouse_id=source.warehouse_id
   AND product_id=source.product_id;
 INSERT INTO one_journal_entries(
   tenant_id,id,period_id,reference,idempotency_key,occurred_on,currency)
 VALUES(p_tenant,p_journal,p_period,'unbilled-vendor-return:'||p_return::text,
   'unbilled-vendor-return:'||p_return::text,current_date,valued.currency);
 INSERT INTO one_journal_lines(
   tenant_id,entry_id,line_number,account_id,debit,credit)
 VALUES(p_tenant,p_journal,1,p_grni,money,0),
       (p_tenant,p_journal,2,p_inventory,0,money);
 PERFORM one_post_journal(p_tenant,p_journal,p_actor);
 INSERT INTO one_stock_value_events(
  tenant_id,stock_event_id,journal_id,movement_kind,qty,amount,
  currency,position_qty_after,position_value_after,actor_id)
 VALUES(p_tenant,p_stock_event,p_journal,'vendor_return',returned.quantity,
  money,valued.currency,new_qty,new_value,p_actor);
 INSERT INTO one_vendor_return_cost_links(
  tenant_id,return_job_id,source_receipt_id,stock_event_id,qty,amount,actor_id)
 VALUES(p_tenant,p_return,source.id,p_stock_event,returned.quantity,money,p_actor);
 RETURN p_return;
END $$;

-- The original supplier billing function is tightened to subtract all
-- vendor_return movements, even legacy unvalued ones, from billable receipts.

CREATE OR REPLACE FUNCTION one_link_goods_vendor_bill(
  p_tenant uuid,p_bill uuid,p_order uuid,p_grni text,
  p_order_lines integer[],p_actor uuid
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE
 v_order one_commercial_orders%ROWTYPE;
 v_bill one_invoice_docs%ROWTYPE;
 v_line one_commercial_order_lines%ROWTYPE;
 v_bill_line one_invoice_lines%ROWTYPE;
 allowed_qty numeric(24,3);
 already_qty numeric(24,3);
 returned_qty numeric(24,3);
 loop_ix integer;
 line_count integer;
BEGIN
 IF p_tenant IS NULL OR p_bill IS NULL OR p_order IS NULL OR
    p_grni IS NULL OR p_actor IS NULL OR p_order_lines IS NULL OR
    cardinality(p_order_lines) NOT BETWEEN 1 AND 1000 OR
    current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
    current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
    NOT EXISTS(SELECT 1 FROM one_memberships m
      WHERE m.tenant_id=p_tenant AND m.actor_id=p_actor AND m.active
      AND m.role IN ('company_admin','accountant')) THEN
  RAISE EXCEPTION 'Authorized, complete supplier goods bill matching required';
 END IF;
 IF EXISTS(SELECT 1 FROM unnest(p_order_lines) AS x(v)
           WHERE v IS NULL OR v<=0) OR
    (SELECT count(DISTINCT v) FROM unnest(p_order_lines) AS x(v))
       <>cardinality(p_order_lines) THEN
  RAISE EXCEPTION 'Unique positive vendor bill source lines required';
 END IF;
 IF NOT EXISTS(SELECT 1 FROM one_accounts
       WHERE tenant_id=p_tenant AND id=p_grni AND kind='liability'
         AND active AND posting_allowed) THEN
  RAISE EXCEPTION 'Goods received clearing account must be active liability';
 END IF;
 -- SERIALIZE invoices claiming the same ordered units by this order lock.
 SELECT * INTO v_order FROM one_commercial_orders
   WHERE tenant_id=p_tenant AND id=p_order FOR UPDATE;
 IF NOT FOUND OR v_order.kind<>'purchase' OR v_order.status<>'confirmed' THEN
  RAISE EXCEPTION 'Confirmed purchase order required for vendor bill';
 END IF;
 SELECT * INTO v_bill FROM one_invoice_docs
   WHERE tenant_id=p_tenant AND id=p_bill FOR UPDATE;
 IF NOT FOUND OR v_bill.kind<>'vendor_bill' OR v_bill.status<>'draft'
    OR v_bill.partner_id<>v_order.partner_id OR
       v_bill.currency<>v_order.currency THEN
  RAISE EXCEPTION 'Issued vendor bill does not match PO supplier/currency';
 END IF;
 SELECT count(*) INTO line_count FROM one_invoice_lines
   WHERE tenant_id=p_tenant AND doc_id=p_bill;
 IF line_count<>cardinality(p_order_lines)
    OR EXISTS(SELECT 1 FROM one_goods_bill_allocations
              WHERE tenant_id=p_tenant AND bill_id=p_bill) THEN
  RAISE EXCEPTION 'Source mapping must cover each invoice line only once';
 END IF;
 FOR loop_ix IN 1..cardinality(p_order_lines) LOOP
  SELECT * INTO v_line FROM one_commercial_order_lines
    WHERE tenant_id=p_tenant AND order_id=p_order
      AND line_no=p_order_lines[loop_ix] FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'Missing PO source line'; END IF;
  SELECT * INTO v_bill_line FROM one_invoice_lines
    WHERE tenant_id=p_tenant AND doc_id=p_bill AND line_number=loop_ix;
  IF NOT FOUND OR v_bill_line.account_id<>p_grni OR
     v_bill_line.quantity<>round(v_bill_line.quantity,3) OR
     v_bill_line.unit_price<>v_line.unit_price OR
     v_bill_line.discount_amount<>0 OR v_bill_line.tax_rate<>0 OR
     v_bill_line.tax_total<>0 OR
     v_bill_line.tax_category NOT IN ('out_of_scope','zero_rated') THEN
   RAISE EXCEPTION 'Goods bill must clear same PO price into GRNI without tax';
  END IF;
  -- ONLY goods physically received AND financially valued are billable in
  -- this first-cut matching bridge. Unvalued receiving cannot be invoiced.
  SELECT coalesce(sum(valued.qty),0) INTO allowed_qty
    FROM one_stock_value_events valued
    JOIN one_stock_events movement
      ON movement.tenant_id=valued.tenant_id
      AND movement.id=valued.stock_event_id
    WHERE movement.tenant_id=p_tenant AND movement.order_id=p_order
      AND movement.line_no=v_line.line_no
      AND movement.movement_kind='purchase_receipt';
  SELECT coalesce(sum(billed_qty),0) INTO already_qty
    FROM one_goods_bill_allocations
    WHERE tenant_id=p_tenant AND order_id=p_order
      AND order_line_no=v_line.line_no;
  -- A supplier-returned physical unit is no longer eligible for an AP bill.
  -- Include legacy vendor_return movements too, never silently overbill.
  SELECT coalesce(sum(quantity),0) INTO returned_qty
    FROM one_stock_events
    WHERE tenant_id=p_tenant AND order_id=p_order
      AND line_no=v_line.line_no AND movement_kind='vendor_return';
  IF allowed_qty-returned_qty<already_qty+v_bill_line.quantity THEN
   RAISE EXCEPTION 'Supplier bill exceeds valued goods received and not yet billed';
  END IF;
  INSERT INTO one_goods_bill_allocations(
    tenant_id,bill_id,bill_line_no,order_id,order_line_no,billed_qty,actor_id)
  VALUES(p_tenant,p_bill,loop_ix,p_order,v_line.line_no,
         v_bill_line.quantity,p_actor);
 END LOOP;
 RETURN p_bill;
END $$;

REVOKE ALL ON one_vendor_return_cost_links FROM PUBLIC;
REVOKE ALL ON FUNCTION
 one_complete_unbilled_vendor_return(uuid,uuid,uuid,uuid,uuid,text,text,uuid)
 FROM PUBLIC;
COMMIT;
