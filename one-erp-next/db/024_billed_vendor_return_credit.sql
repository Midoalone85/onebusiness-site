-- ONE ERP Next / 024 | billed vendor return -> AP credit + Inventory credit.
-- PostgreSQL 14 isolated CI. No VAT/FX/landed-cost or official e-invoice.
-- Never grant direct table DML/GUC context to an untrusted application user.
BEGIN;

CREATE TABLE one_billed_vendor_return_credit_links (
 tenant_id uuid NOT NULL,
 return_job_id uuid NOT NULL,
 source_receipt_id uuid NOT NULL,
 source_bill_id uuid NOT NULL,
 source_bill_line_no integer NOT NULL CHECK(source_bill_line_no>0),
 credit_invoice_id uuid NOT NULL,
 stock_event_id uuid NOT NULL,
 quantity numeric(20,3) NOT NULL CHECK(quantity>0),
 amount numeric(20,3) NOT NULL CHECK(amount>0),
 actor_id uuid NOT NULL,
 created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 PRIMARY KEY(tenant_id,return_job_id),
 UNIQUE(tenant_id,credit_invoice_id),
 UNIQUE(tenant_id,stock_event_id),
 FOREIGN KEY(tenant_id,return_job_id)
   REFERENCES one_return_jobs(tenant_id,id),
 FOREIGN KEY(tenant_id,source_receipt_id)
   REFERENCES one_stock_events(tenant_id,id),
 FOREIGN KEY(tenant_id,source_bill_id,source_bill_line_no)
   REFERENCES one_invoice_lines(tenant_id,doc_id,line_number),
 FOREIGN KEY(tenant_id,credit_invoice_id)
   REFERENCES one_invoice_docs(tenant_id,id),
 FOREIGN KEY(tenant_id,stock_event_id)
   REFERENCES one_stock_value_events(tenant_id,stock_event_id)
);
CREATE INDEX one_billed_return_by_bill_line
  ON one_billed_vendor_return_credit_links
  (tenant_id,source_bill_id,source_bill_line_no);
ALTER TABLE one_billed_vendor_return_credit_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_billed_vendor_return_credit_links FORCE ROW LEVEL SECURITY;
CREATE POLICY one_billed_return_credit_tenant
 ON one_billed_vendor_return_credit_links
 USING(tenant_id::text=current_setting('one.tenant_id',true))
 WITH CHECK(tenant_id::text=current_setting('one.tenant_id',true));
CREATE TRIGGER one_billed_return_credit_append_only
 BEFORE UPDATE OR DELETE ON one_billed_vendor_return_credit_links
 FOR EACH ROW EXECUTE FUNCTION one_protect_audit();

-- All of the physical movement, valued stock, vendor credit, posted GL
-- and immutable source links either commit together or all roll back.
-- Restricted one-receipt-per-PO-line pilot: no ambiguous source attribution.
CREATE FUNCTION one_complete_billed_vendor_return(
 p_tenant uuid,p_return uuid,p_bill uuid,p_bill_line integer,
 p_credit uuid,p_credit_number text,p_event uuid,p_period uuid,
 p_inventory text,p_ap text,p_actor uuid,p_on date
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE
 source_order uuid;
 r one_return_jobs%ROWTYPE;
 rec one_stock_events%ROWTYPE;
 receipt_value one_stock_value_events%ROWTYPE;
 b one_invoice_docs%ROWTYPE;
 bill_line one_invoice_lines%ROWTYPE;
 alloc one_goods_bill_allocations%ROWTYPE;
 pos one_stock_value_positions%ROWTYPE;
 phys one_stock_balances%ROWTYPE;
 ord one_commercial_orders%ROWTYPE;
 currency_digits integer;
 bill_total_qty numeric(24,3);
 credit_used_qty numeric(24,3);
 source_used_qty numeric(24,3);
 stock_qty_after numeric(20,3);
 stock_value_after numeric(20,3);
 unit_cost numeric(20,3);
 book_cost numeric(20,3);
 billed_amount numeric(20,3);
 event_count bigint;
BEGIN
 IF p_tenant IS NULL OR p_return IS NULL OR p_bill IS NULL OR
    p_bill_line IS NULL OR p_bill_line<=0 OR
    p_credit IS NULL OR p_event IS NULL OR p_period IS NULL OR
    p_actor IS NULL OR p_on IS NULL OR p_credit_number IS NULL OR
    length(btrim(p_credit_number)) NOT BETWEEN 1 AND 100 OR
    p_inventory IS NULL OR p_ap IS NULL OR p_inventory=p_ap OR
    p_event=p_credit OR
    current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
    current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
    NOT EXISTS(SELECT 1 FROM one_memberships m
      WHERE m.tenant_id=p_tenant AND m.actor_id=p_actor AND m.active
        AND m.role='company_admin') THEN
  RAISE EXCEPTION 'Verified administrator and linked billed return required';
 END IF;
 SELECT s.order_id INTO source_order
   FROM one_return_jobs j JOIN one_stock_events s
      ON s.tenant_id=j.tenant_id AND s.id=j.source_event_id
   WHERE j.tenant_id=p_tenant AND j.id=p_return;
 IF source_order IS NULL THEN RAISE EXCEPTION 'Unknown original receipt'; END IF;

 -- Common commercial lock order. A purchase invoice and a return both
 -- claim original PO-line quantities, never accept an uncommitted snapshot.
 SELECT * INTO ord FROM one_commercial_orders
   WHERE tenant_id=p_tenant AND id=source_order FOR UPDATE;
 IF NOT FOUND OR ord.kind<>'purchase' OR ord.status<>'confirmed' THEN
  RAISE EXCEPTION 'Confirmed PO needed for billed return';
 END IF;
 SELECT * INTO r FROM one_return_jobs
   WHERE tenant_id=p_tenant AND id=p_return FOR UPDATE;
 IF NOT FOUND OR r.return_kind<>'vendor' OR r.status<>'requested'
     OR r.quantity<=0 THEN
  RAISE EXCEPTION 'Requested vendor RMA only, no duplicate processing';
 END IF;
 SELECT * INTO rec FROM one_stock_events
   WHERE tenant_id=p_tenant AND id=r.source_event_id FOR UPDATE;
 IF NOT FOUND OR rec.order_id<>source_order OR
    rec.movement_kind<>'purchase_receipt' THEN
  RAISE EXCEPTION 'Return must belong to original purchased receipt';
 END IF;
 SELECT * INTO receipt_value FROM one_stock_value_events
   WHERE tenant_id=p_tenant AND stock_event_id=rec.id FOR UPDATE;
 IF NOT FOUND OR receipt_value.movement_kind<>'purchase_receipt'
    OR receipt_value.qty<>rec.quantity THEN
  RAISE EXCEPTION 'Original physical receipt must already be valued';
 END IF;
 -- The existing goods-bill allocations link a bill to PO line, not to each
 -- of multiple physical receipts. Reject that ambiguous case until receipt-
 -- specific allocations are implemented.
 SELECT count(*) INTO event_count FROM one_stock_events
  WHERE tenant_id=p_tenant AND order_id=rec.order_id
    AND line_no=rec.line_no AND movement_kind='purchase_receipt';
 IF event_count<>1 THEN
  RAISE EXCEPTION 'Ambiguous multiple receipt allocations not supported';
 END IF;
 SELECT * INTO b FROM one_invoice_docs
   WHERE tenant_id=p_tenant AND id=p_bill FOR UPDATE;
 IF NOT FOUND OR b.kind<>'vendor_bill' OR b.status<>'issued'
    OR b.partner_id<>ord.partner_id OR b.currency<>ord.currency
    OR b.currency<>receipt_value.currency OR p_on<b.issued_on THEN
  RAISE EXCEPTION 'Issued matching supplier goods bill required';
 END IF;
 SELECT * INTO bill_line FROM one_invoice_lines
   WHERE tenant_id=p_tenant AND doc_id=p_bill AND line_number=p_bill_line;
 SELECT * INTO alloc FROM one_goods_bill_allocations
   WHERE tenant_id=p_tenant AND bill_id=p_bill AND bill_line_no=p_bill_line;
 IF NOT FOUND OR alloc.order_id<>rec.order_id
    OR alloc.order_line_no<>rec.line_no
    OR bill_line.doc_id IS NULL OR
    bill_line.quantity<>alloc.billed_qty OR bill_line.quantity<=0
    OR bill_line.unit_price<=0 OR bill_line.discount_amount<>0
    OR bill_line.tax_total<>0 OR bill_line.tax_rate<>0
    OR bill_line.input_tax_recoverable
    OR bill_line.tax_category NOT IN ('out_of_scope','zero_rated') THEN
  RAISE EXCEPTION 'Original issued GRNI bill line allocation or tax unsupported';
 END IF;
 SELECT minor_units INTO currency_digits
   FROM one_currency_precision WHERE code=b.currency;
 IF currency_digits IS NULL OR
    bill_line.unit_price<>round(bill_line.unit_price,currency_digits) OR
    bill_line.base_total<>round(bill_line.quantity*bill_line.unit_price,currency_digits) THEN
  RAISE EXCEPTION 'Invoice precision or original cost invalid';
 END IF;
 IF NOT EXISTS(SELECT 1 FROM one_accounts
      WHERE tenant_id=p_tenant AND id=bill_line.account_id
        AND kind='liability' AND active AND posting_allowed)
    OR NOT EXISTS(SELECT 1 FROM one_accounts
      WHERE tenant_id=p_tenant AND id=p_inventory
        AND kind='asset' AND active AND posting_allowed)
    OR NOT EXISTS(SELECT 1 FROM one_accounts
      WHERE tenant_id=p_tenant AND id=p_ap
        AND kind='liability' AND active AND posting_allowed)
    OR b.control_account_id<>p_ap THEN
  RAISE EXCEPTION 'Inventory/AP and original GRNI account source required';
 END IF;
 -- The matched PO bill quantity is constrained to a single invoice line.
 -- Another billed RMA cannot claim these source invoice units twice.
 SELECT coalesce(sum(quantity),0) INTO credit_used_qty
  FROM one_billed_vendor_return_credit_links
  WHERE tenant_id=p_tenant AND source_bill_id=p_bill
    AND source_bill_line_no=p_bill_line;
 IF credit_used_qty+r.quantity>alloc.billed_qty THEN
  RAISE EXCEPTION 'Supplier credit exceeds original billed units';
 END IF;
 SELECT coalesce(sum(billed_qty),0) INTO bill_total_qty
  FROM one_goods_bill_allocations
  WHERE tenant_id=p_tenant AND order_id=rec.order_id
    AND order_line_no=rec.line_no;
 SELECT coalesce(sum(quantity),0) INTO source_used_qty
  FROM one_billed_vendor_return_credit_links l
   JOIN one_stock_events ev ON ev.tenant_id=l.tenant_id
     AND ev.id=l.source_receipt_id
  WHERE l.tenant_id=p_tenant AND ev.order_id=rec.order_id
    AND ev.line_no=rec.line_no;
 IF source_used_qty+r.quantity>bill_total_qty THEN
  RAISE EXCEPTION 'Supplier credit has no unmatched billed source quantity';
 END IF;

 SELECT * INTO pos FROM one_stock_value_positions
  WHERE tenant_id=p_tenant AND warehouse_id=rec.warehouse_id
    AND product_id=rec.product_id FOR UPDATE;
 SELECT * INTO phys FROM one_stock_balances
  WHERE tenant_id=p_tenant AND warehouse_id=rec.warehouse_id
    AND product_id=rec.product_id FOR UPDATE;
 IF pos.tenant_id IS NULL OR phys.tenant_id IS NULL OR
    pos.qty<=0 OR pos.qty<>phys.on_hand OR
    pos.qty<r.quantity OR phys.on_hand-phys.reserved<r.quantity OR
    pos.currency<>b.currency THEN
  RAISE EXCEPTION 'Unreserved physical quantity must match valued inventory';
 END IF;
 -- Refuse any earlier legacy movement that was not financially valued.
 IF EXISTS(SELECT 1 FROM one_stock_events ev
     WHERE ev.tenant_id=p_tenant AND ev.warehouse_id=rec.warehouse_id
       AND ev.product_id=rec.product_id
       AND NOT EXISTS(SELECT 1 FROM one_stock_value_events val
         WHERE val.tenant_id=ev.tenant_id AND val.stock_event_id=ev.id)
       AND NOT EXISTS(SELECT 1 FROM one_return_disposition_costs c
         JOIN one_return_dispositions d
           ON d.tenant_id=c.tenant_id AND d.id=c.disposition_id
         WHERE c.tenant_id=ev.tenant_id
           AND (d.stock_event_out_id=ev.id OR d.stock_event_in_id=ev.id))
    ) THEN
  RAISE EXCEPTION 'Unvalued movement requires reconciliation first';
 END IF;
 billed_amount=round(r.quantity*bill_line.unit_price,currency_digits);
 unit_cost=round(receipt_value.amount*r.quantity/receipt_value.qty,
                 currency_digits);
 book_cost=CASE WHEN r.quantity=pos.qty THEN pos.book_value
     ELSE round(pos.book_value*r.quantity/pos.qty,currency_digits) END;
 stock_qty_after=pos.qty-r.quantity;
 stock_value_after=pos.book_value-book_cost;
 IF billed_amount<=0 OR billed_amount<>unit_cost OR
    billed_amount<>book_cost OR stock_value_after<0 OR
    (stock_qty_after=0 AND stock_value_after<>0) OR
    (stock_qty_after>0 AND stock_value_after<=0) THEN
  RAISE EXCEPTION 'Unapproved supplier price/stock cost variance: stop';
 END IF;

 -- Original return operation enforces request lock, available stock and
 -- immutable physical/audit event; failure at any later step rolls it back.
 PERFORM one_finish_return(p_tenant,p_return,r.quantity,p_event,p_actor);
 SELECT * INTO phys FROM one_stock_balances
  WHERE tenant_id=p_tenant AND warehouse_id=rec.warehouse_id
    AND product_id=rec.product_id FOR UPDATE;
 IF phys.on_hand<>stock_qty_after THEN
  RAISE EXCEPTION 'Post-return physical balance drift';
 END IF;
 PERFORM set_config('one.valuation_context','yes',true);
 UPDATE one_stock_value_positions SET qty=stock_qty_after,
   book_value=stock_value_after
 WHERE tenant_id=p_tenant AND warehouse_id=rec.warehouse_id
   AND product_id=rec.product_id;

 -- A vendor-credit's GL is NOT the normal expense credit. The internal
 -- original goods purchase already debited inventory via received goods.
 INSERT INTO one_journal_entries
   (tenant_id,id,period_id,reference,idempotency_key,occurred_on,currency)
 VALUES(p_tenant,p_credit,p_period,'vendor_credit:'||btrim(p_credit_number),
   'invoice:'||p_credit::text,p_on,b.currency);
 INSERT INTO one_journal_lines
   (tenant_id,entry_id,line_number,account_id,debit,credit)
 VALUES(p_tenant,p_credit,1,p_ap,billed_amount,0),
       (p_tenant,p_credit,2,p_inventory,0,book_cost);
 PERFORM one_post_journal(p_tenant,p_credit,p_actor);
 INSERT INTO one_stock_value_events
   (tenant_id,stock_event_id,journal_id,movement_kind,qty,amount,
    currency,position_qty_after,position_value_after,actor_id)
 VALUES(p_tenant,p_event,p_credit,'vendor_return',r.quantity,
   book_cost,b.currency,stock_qty_after,stock_value_after,p_actor);

 INSERT INTO one_invoice_docs
   (tenant_id,id,kind,number,partner_id,issued_on,due_on,currency,
    source_invoice_id,control_account_id,journal_id,net_total,tax_total,gross_total)
 VALUES(p_tenant,p_credit,'vendor_credit',btrim(p_credit_number),
   b.partner_id,p_on,p_on,b.currency,p_bill,p_ap,p_credit,
   billed_amount,0,billed_amount);
 INSERT INTO one_invoice_lines
   (tenant_id,doc_id,line_number,description,account_id,quantity,unit_price,
    discount_amount,tax_category,tax_code,tax_rate,input_tax_recoverable,
    base_total,tax_total)
 VALUES(p_tenant,p_credit,1,'Physical return of billed purchase',
   p_inventory,r.quantity,bill_line.unit_price,0,bill_line.tax_category,
   bill_line.tax_code,0,false,billed_amount,0);
 INSERT INTO one_billed_vendor_return_credit_links
   (tenant_id,return_job_id,source_receipt_id,source_bill_id,
    source_bill_line_no,credit_invoice_id,stock_event_id,quantity,amount,actor_id)
 VALUES(p_tenant,p_return,rec.id,p_bill,p_bill_line,p_credit,p_event,
   r.quantity,billed_amount,p_actor);

 -- This controlled action validates the complete bill/return/stock snapshot
 -- above and seals the draft itself. Generic issuer intentionally still
 -- refuses inventory-asset lines for an ordinary vendor credit.
 PERFORM set_config('one.invoice_context','yes',true);
 UPDATE one_invoice_docs SET status='issued',
   approved_at=transaction_timestamp(),approved_by=p_actor
 WHERE tenant_id=p_tenant AND id=p_credit;
 PERFORM set_config('one.invoice_context','',true);
 INSERT INTO one_invoice_audit(tenant_id,doc_id,actor_id)
 VALUES(p_tenant,p_credit,p_actor);
 RETURN p_credit;
END $$;

REVOKE ALL ON one_billed_vendor_return_credit_links FROM PUBLIC;
REVOKE ALL ON FUNCTION one_complete_billed_vendor_return(
 uuid,uuid,uuid,integer,uuid,text,uuid,uuid,text,text,uuid,date
) FROM PUBLIC;

-- Link RMA timeline to the posted AP credit without changing earlier events.
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
        SELECT 1 FROM one_vendor_return_cost_links vr
        JOIN one_stock_value_events val
          ON val.tenant_id=vr.tenant_id
         AND val.stock_event_id=vr.stock_event_id
        WHERE vr.tenant_id=j.tenant_id AND vr.return_job_id=j.id
          AND o.source_id=val.journal_id
          AND o.event_type='finance.journal_posted'
      )
      OR EXISTS(
         SELECT 1 FROM one_billed_vendor_return_credit_links br
         WHERE br.tenant_id=j.tenant_id AND br.return_job_id=j.id
           AND (
              (o.source_id=br.credit_invoice_id AND
                 o.event_type IN ('finance.invoice_issued','finance.journal_posted'))
              OR (o.source_id=br.source_bill_id AND
                 o.event_type='finance.invoice_issued')
           )
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


-- GRNI recognizes only UNBILLED returns. A BILLED return offsets AP,
-- not GRNI, even though both are stored as vendor_return stock events.
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
  (received.total_value-billed.total_value-
    vendor_returned.total_value)::numeric,
  (missed.unvalued_count=0
   AND b.on_hand=coalesce(v.qty,0)
   AND received.total_value>=billed.total_value+
       vendor_returned.total_value)
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
  SELECT coalesce(sum(vs.amount),0)::numeric AS total_value
  FROM one_stock_value_events vs
  JOIN one_stock_events e ON e.tenant_id=vs.tenant_id
   AND e.id=vs.stock_event_id
  WHERE e.tenant_id=b.tenant_id AND e.warehouse_id=b.warehouse_id
   AND e.product_id=b.product_id AND e.movement_kind='vendor_return'
    AND EXISTS(SELECT 1 FROM one_vendor_return_cost_links l
      WHERE l.tenant_id=vs.tenant_id AND l.stock_event_id=vs.stock_event_id)
 ) vendor_returned
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
