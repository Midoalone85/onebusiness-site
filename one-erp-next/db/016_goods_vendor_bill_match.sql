-- ONE ERP Next 016 | 3-way goods bill matching (PO -> valued receipt -> AP)
-- PostgreSQL 14 disposable CI prototype. Strict no-tax/no-discount contract.
BEGIN;
CREATE TABLE one_goods_bill_allocations (
  tenant_id uuid NOT NULL,
  bill_id uuid NOT NULL,
  bill_line_no integer NOT NULL CHECK(bill_line_no>0),
  order_id uuid NOT NULL,
  order_line_no integer NOT NULL CHECK(order_line_no>0),
  billed_qty numeric(20,3) NOT NULL CHECK(billed_qty>0),
  actor_id uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
  PRIMARY KEY(tenant_id,bill_id,bill_line_no),
  FOREIGN KEY(tenant_id,bill_id,bill_line_no)
    REFERENCES one_invoice_lines(tenant_id,doc_id,line_number),
  FOREIGN KEY(tenant_id,order_id,order_line_no)
    REFERENCES one_commercial_order_lines(tenant_id,order_id,line_no)
);
CREATE INDEX one_goods_bill_alloc_source_idx
  ON one_goods_bill_allocations(tenant_id,order_id,order_line_no);
ALTER TABLE one_goods_bill_allocations ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_goods_bill_allocations FORCE ROW LEVEL SECURITY;
CREATE POLICY one_goods_bill_alloc_scope ON one_goods_bill_allocations
 USING(tenant_id::text=current_setting('one.tenant_id',true))
 WITH CHECK(tenant_id::text=current_setting('one.tenant_id',true));
CREATE TRIGGER one_goods_bill_alloc_immutable BEFORE UPDATE OR DELETE
 ON one_goods_bill_allocations FOR EACH ROW EXECUTE FUNCTION one_protect_audit();

CREATE FUNCTION one_link_goods_vendor_bill(
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
  IF allowed_qty<already_qty+v_bill_line.quantity THEN
   RAISE EXCEPTION 'Supplier bill exceeds valued goods received and not yet billed';
  END IF;
  INSERT INTO one_goods_bill_allocations(
    tenant_id,bill_id,bill_line_no,order_id,order_line_no,billed_qty,actor_id)
  VALUES(p_tenant,p_bill,loop_ix,p_order,v_line.line_no,
         v_bill_line.quantity,p_actor);
 END LOOP;
 RETURN p_bill;
END $$;
-- The invoice issuer permits a GRNI-liability line ONLY IF its authoritative
-- physical + PO allocation already exists at the point of issuing. This is a
-- real DB invariant, not a spoofable SQL session flag.
CREATE OR REPLACE FUNCTION one_issue_invoice(p_tenant uuid,p_doc uuid,p_actor uuid)
RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE
    d one_invoice_docs%ROWTYPE;
    j one_journal_entries%ROWTYPE;
    source_doc one_invoice_docs%ROWTYPE;
    n integer;
    bases numeric(24,3);
    taxes numeric(24,3);
    scale smallint;
    prior_credited numeric(24,3);
    control_count integer;
    expected_kind text;
BEGIN
    IF current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text
       OR current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text
       OR NOT EXISTS (
         SELECT 1 FROM one_memberships
         WHERE tenant_id=p_tenant AND actor_id=p_actor AND active
           AND role IN ('accountant','company_admin')
       ) THEN
        RAISE EXCEPTION 'Authorized accounting user required';
    END IF;
    SELECT * INTO d FROM one_invoice_docs
      WHERE tenant_id=p_tenant AND id=p_doc FOR UPDATE;
    IF NOT FOUND OR d.status<>'draft' THEN
        RAISE EXCEPTION 'Invoice missing or already issued';
    END IF;
    SELECT * INTO j FROM one_journal_entries
      WHERE tenant_id=p_tenant AND id=d.journal_id FOR UPDATE;
    IF NOT FOUND OR j.status<>'posted' OR j.currency<>d.currency OR
       j.occurred_on<>d.issued_on OR
       j.reference<>d.kind||':'||d.number OR
       j.idempotency_key<>'invoice:'||d.id::text THEN
        RAISE EXCEPTION 'Linked posted journal must match invoice identity';
    END IF;
    SELECT minor_units INTO scale FROM one_currency_precision WHERE code=d.currency;
    SELECT count(*),coalesce(sum(base_total),0),coalesce(sum(tax_total),0)
      INTO n,bases,taxes FROM one_invoice_lines
      WHERE tenant_id=p_tenant AND doc_id=d.id;
    IF n<1 OR bases<>d.net_total OR taxes<>d.tax_total OR
       EXISTS (SELECT 1 FROM one_invoice_lines l
               WHERE l.tenant_id=p_tenant AND l.doc_id=d.id AND (
                   l.base_total<>round(l.quantity*l.unit_price-l.discount_amount,scale)
                   OR l.tax_total<>round(l.base_total*l.tax_rate,scale)
                   OR l.unit_price<>round(l.unit_price,scale)
                   OR l.discount_amount<>round(l.discount_amount,scale)
                   OR (l.tax_category='standard' AND l.tax_code='NONE')
                   OR (d.kind IN ('customer_invoice','customer_credit')
                       AND l.input_tax_recoverable)
               )) OR
       EXISTS (SELECT 1 FROM one_invoice_lines l JOIN one_accounts a
               ON a.tenant_id=l.tenant_id AND a.id=l.account_id
               WHERE l.tenant_id=p_tenant AND l.doc_id=d.id AND
                  (NOT a.active OR NOT a.posting_allowed OR
                   (a.kind<>CASE WHEN d.kind IN
                     ('customer_invoice','customer_credit') THEN 'revenue'
                     ELSE 'expense' END
                   AND NOT (d.kind='vendor_bill'
                     AND a.kind='liability'
                     AND EXISTS(SELECT 1 FROM one_goods_bill_allocations alloc
                       WHERE alloc.tenant_id=l.tenant_id
                         AND alloc.bill_id=l.doc_id
                         AND alloc.bill_line_no=l.line_number))))) OR
       EXISTS (SELECT 1 FROM one_journal_lines l WHERE l.tenant_id=p_tenant
               AND l.entry_id=d.journal_id AND
               (l.debit<>round(l.debit,scale) OR l.credit<>round(l.credit,scale)))
    THEN
        RAISE EXCEPTION 'Invoice line and tax snapshot mismatch';
    END IF;
    IF (SELECT coalesce(sum(debit),0) FROM one_journal_lines
         WHERE tenant_id=p_tenant AND entry_id=d.journal_id)<>d.gross_total THEN
        RAISE EXCEPTION 'Journal posting total differs from invoice';
    END IF;
    expected_kind := CASE WHEN d.kind IN ('customer_invoice','customer_credit')
                          THEN 'asset' ELSE 'liability' END;
    IF NOT EXISTS (
        SELECT 1 FROM one_accounts a WHERE a.tenant_id=p_tenant
          AND a.id=d.control_account_id AND a.kind=expected_kind
          AND a.active AND a.posting_allowed
    ) THEN
        RAISE EXCEPTION 'Invalid invoice control account';
    END IF;
    SELECT count(*) INTO control_count FROM one_journal_lines l
       WHERE l.tenant_id=p_tenant AND l.entry_id=d.journal_id
         AND l.account_id=d.control_account_id
         AND CASE WHEN d.kind IN ('customer_invoice','vendor_credit')
             THEN l.debit=d.gross_total AND l.credit=0
             ELSE l.credit=d.gross_total AND l.debit=0 END;
    IF control_count<>1 THEN
        RAISE EXCEPTION 'Invoice control balance absent or incorrect';
    END IF;
    IF d.source_invoice_id IS NOT NULL THEN
        -- Lock original to serialize partial-credit issuance across sessions.
        SELECT * INTO source_doc FROM one_invoice_docs
          WHERE tenant_id=p_tenant AND id=d.source_invoice_id FOR UPDATE;
        IF NOT FOUND OR source_doc.status<>'issued' OR
           source_doc.partner_id<>d.partner_id OR
           source_doc.currency<>d.currency OR
           source_doc.issued_on>d.issued_on OR
           (d.kind='customer_credit' AND source_doc.kind<>'customer_invoice') OR
           (d.kind='vendor_credit' AND source_doc.kind<>'vendor_bill') THEN
            RAISE EXCEPTION 'Credit note must reference issued compatible source';
        END IF;
        SELECT coalesce(sum(gross_total),0) INTO prior_credited
          FROM one_invoice_docs
          WHERE tenant_id=p_tenant AND source_invoice_id=d.source_invoice_id
            AND status='issued';
        IF prior_credited+d.gross_total>source_doc.gross_total THEN
            RAISE EXCEPTION 'Total issued credits exceed original invoice';
        END IF;
    END IF;
    PERFORM set_config('one.invoice_context','yes',true);
    UPDATE one_invoice_docs SET status='issued',
        approved_at=transaction_timestamp(),approved_by=p_actor
      WHERE tenant_id=p_tenant AND id=p_doc;
    PERFORM set_config('one.invoice_context','',true);
    INSERT INTO one_invoice_audit(tenant_id,doc_id,actor_id)
      VALUES (p_tenant,p_doc,p_actor);
    RETURN p_doc;
END $$;

REVOKE ALL ON one_goods_bill_allocations FROM PUBLIC;
REVOKE ALL ON FUNCTION
 one_link_goods_vendor_bill(uuid,uuid,uuid,text,integer[],uuid) FROM PUBLIC;
COMMIT;
