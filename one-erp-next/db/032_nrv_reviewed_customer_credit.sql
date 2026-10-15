-- ONE ERP NEXT / 032 | Finance-linked NRV return customer credit, internal only.
-- Disposable PostgreSQL 14 CI. Does not issue legal/ZATCA tax credit notes,
-- transmit documents, process payments, or release quarantined goods.
BEGIN;

CREATE TABLE one_nrv_customer_return_credit_links (
  tenant_id uuid NOT NULL,
  return_job_id uuid NOT NULL,
  original_invoice_id uuid NOT NULL,
  credit_invoice_id uuid NOT NULL,
  reviewed_nrv_event_id uuid NOT NULL,
  approved_qty numeric(20,3) NOT NULL CHECK(approved_qty>0),
  issued_net numeric(20,3) NOT NULL CHECK(issued_net>0),
  actor_id uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
  PRIMARY KEY(tenant_id,return_job_id),
  UNIQUE(tenant_id,credit_invoice_id),
  FOREIGN KEY(tenant_id,return_job_id)
    REFERENCES one_nrv_customer_return_reviews(tenant_id,return_job_id),
  FOREIGN KEY(tenant_id,original_invoice_id)
    REFERENCES one_invoice_docs(tenant_id,id),
  FOREIGN KEY(tenant_id,credit_invoice_id)
    REFERENCES one_invoice_docs(tenant_id,id),
  FOREIGN KEY(tenant_id,reviewed_nrv_event_id)
    REFERENCES one_inventory_nrv_events(tenant_id,id),
  CHECK(credit_invoice_id<>original_invoice_id)
);
ALTER TABLE one_nrv_customer_return_credit_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_nrv_customer_return_credit_links FORCE ROW LEVEL SECURITY;
CREATE POLICY nrv_customer_credit_scope ON one_nrv_customer_return_credit_links
 USING(tenant_id::text=current_setting('one.tenant_id',true))
 WITH CHECK(tenant_id::text=current_setting('one.tenant_id',true));
CREATE TRIGGER one_nrv_customer_credit_append_only BEFORE UPDATE OR DELETE
 ON one_nrv_customer_return_credit_links FOR EACH ROW
 EXECUTE FUNCTION one_protect_audit();

CREATE FUNCTION one_issue_reviewed_nrv_customer_credit(
  p_tenant uuid,p_return uuid,p_original uuid,p_credit uuid,
  p_number text,p_period uuid,p_ar text,p_revenue text,
  p_actor uuid,p_issued_on date
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE
  r one_return_jobs%ROWTYPE;
  review one_nrv_customer_return_reviews%ROWTYPE;
  original one_invoice_docs%ROWTYPE;
  original_line one_invoice_lines%ROWTYPE;
  alloc one_sales_invoice_allocations%ROWTYPE;
  source one_stock_events%ROWTYPE;
  report_check record;
  mscale integer;
  amount numeric(20,3);
  prior_credit_qty numeric(24,3);
  source_invoice_line_count integer;
BEGIN
  IF p_tenant IS NULL OR p_return IS NULL OR p_original IS NULL
     OR p_credit IS NULL OR p_credit=p_original OR p_period IS NULL
     OR p_actor IS NULL OR p_issued_on IS NULL
     OR p_number IS NULL OR length(btrim(p_number)) NOT BETWEEN 1 AND 100
     OR p_ar IS NULL OR p_revenue IS NULL OR p_ar=p_revenue
     OR current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text
     OR current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text
     OR NOT EXISTS(SELECT 1 FROM one_memberships m
        WHERE m.tenant_id=p_tenant AND m.actor_id=p_actor
          AND m.active AND m.role IN ('company_admin','accountant')) THEN
    RAISE EXCEPTION 'Authorized scoped customer credit required';
  END IF;
  -- Snapshot and idempotency gate is serialized on the accepted RMA.
  SELECT * INTO r FROM one_return_jobs
    WHERE tenant_id=p_tenant AND id=p_return FOR UPDATE;
  IF NOT FOUND OR r.return_kind<>'customer' OR r.status<>'completed'
     OR r.accepted_qty<=0 OR r.processed_event_id IS NULL THEN
    RAISE EXCEPTION 'Completed accepted customer return required';
  END IF;
  SELECT * INTO review FROM one_nrv_customer_return_reviews
    WHERE tenant_id=p_tenant AND return_job_id=p_return FOR UPDATE;
  IF NOT FOUND OR review.quantity<>r.accepted_qty
     OR review.physical_return_event_id<>r.processed_event_id THEN
    RAISE EXCEPTION 'Independent reviewed QA source missing';
  END IF;
  IF EXISTS(SELECT 1 FROM one_nrv_customer_return_credit_links
    WHERE tenant_id=p_tenant AND return_job_id=p_return) THEN
    RAISE EXCEPTION 'This assessed RMA already has a customer credit';
  END IF;
  -- Verify current trusted QA evidence, not values supplied by a browser.
  SELECT * INTO report_check FROM one_nrv_qa_return_check(
      p_tenant,p_actor,p_return);
  IF NOT FOUND OR report_check.source_reconciled IS DISTINCT FROM true
     OR report_check.original_qty<>r.accepted_qty
     OR report_check.new_allowance<>review.new_allowance
     OR report_check.qa_net_value<>review.reassessed_nrv THEN
    RAISE EXCEPTION 'QA inventory/NRV/GL source reconciliation failed';
  END IF;
  SELECT * INTO source FROM one_stock_events
    WHERE tenant_id=p_tenant AND id=r.source_event_id;
  IF NOT FOUND OR source.movement_kind<>'sale_dispatch'
     OR source.id<>review.original_sold_event_id THEN
    RAISE EXCEPTION 'Reviewed RMA does not match original dispatched goods';
  END IF;
  SELECT * INTO original FROM one_invoice_docs
    WHERE tenant_id=p_tenant AND id=p_original FOR UPDATE;
  IF NOT FOUND OR original.kind<>'customer_invoice'
     OR original.status<>'issued' OR original.issued_on>p_issued_on
     OR original.tax_total<>0 OR original.control_account_id<>p_ar THEN
    RAISE EXCEPTION 'Issued and non-taxable original sales invoice required';
  END IF;
  IF NOT EXISTS(SELECT 1 FROM one_accounts
        WHERE tenant_id=p_tenant AND id=p_ar AND kind='asset'
          AND active AND posting_allowed)
     OR NOT EXISTS(SELECT 1 FROM one_accounts
        WHERE tenant_id=p_tenant AND id=p_revenue AND kind='revenue'
          AND active AND posting_allowed) THEN
    RAISE EXCEPTION 'Approved original revenue and receivable accounts required';
  END IF;
  SELECT count(*) INTO source_invoice_line_count FROM one_invoice_lines
    WHERE tenant_id=p_tenant AND doc_id=p_original;
  IF source_invoice_line_count<>1 THEN
    RAISE EXCEPTION 'Single-line no-tax original invoice only in pilot';
  END IF;
  SELECT * INTO alloc FROM one_sales_invoice_allocations
    WHERE tenant_id=p_tenant AND invoice_id=p_original
      AND order_id=source.order_id AND order_line_no=source.line_no;
  IF NOT FOUND OR alloc.billed_qty<r.accepted_qty OR
     alloc.invoice_line_number<>1 THEN
    RAISE EXCEPTION 'Original sales invoice not matched to accepted returned units';
  END IF;
  SELECT * INTO original_line FROM one_invoice_lines
    WHERE tenant_id=p_tenant AND doc_id=p_original AND line_number=1;
  IF NOT FOUND OR original_line.account_id<>p_revenue OR
     original_line.quantity<>alloc.billed_qty OR
     original_line.quantity<=0 OR original_line.unit_price<=0 OR
     original_line.discount_amount<>0 OR
     original_line.tax_category<>'out_of_scope' OR
     original_line.tax_rate<>0 OR original_line.tax_total<>0 OR
     original_line.tax_code<>'NONE' OR
     original_line.input_tax_recoverable OR
     original.currency IS DISTINCT FROM (
        SELECT currency FROM one_stock_value_events
          WHERE tenant_id=p_tenant AND stock_event_id=source.id
     ) THEN
    RAISE EXCEPTION 'Unsupported taxable, discounted or cost-mismatched source';
  END IF;
  SELECT minor_units INTO mscale FROM one_currency_precision
    WHERE code=original.currency;
  IF mscale IS NULL OR
     original_line.unit_price<>round(original_line.unit_price,mscale) THEN
    RAISE EXCEPTION 'Source currency precision unsupported';
  END IF;
  -- Credits of this invoice can relate to other RMAs. The standard invoice
  -- issuer checks the entire amount ceiling and payment settlement after
  -- the source invoice is locked here. The source link checks quantities.
  SELECT coalesce(sum(c.quantity),0) INTO prior_credit_qty
    FROM one_customer_return_credits c
    JOIN one_return_jobs j ON j.tenant_id=c.tenant_id
      AND j.id=c.return_job_id
    JOIN one_stock_events s ON s.tenant_id=j.tenant_id AND s.id=j.source_event_id
    WHERE c.tenant_id=p_tenant AND c.original_invoice_id=p_original
      AND s.order_id=source.order_id AND s.line_no=source.line_no;
  IF prior_credit_qty+r.accepted_qty>alloc.billed_qty THEN
    RAISE EXCEPTION 'Original billed quantity already fully credited';
  END IF;
  amount=round(original_line.unit_price*r.accepted_qty,mscale);
  IF amount<=0 OR amount>original.gross_total THEN
    RAISE EXCEPTION 'Customer credit amount invalid';
  END IF;

  INSERT INTO one_journal_entries(
      tenant_id,id,period_id,reference,idempotency_key,occurred_on,currency)
    VALUES(p_tenant,p_credit,p_period,
      'customer_credit:'||btrim(p_number),'invoice:'||p_credit::text,
      p_issued_on,original.currency);
  INSERT INTO one_journal_lines(
      tenant_id,entry_id,line_number,account_id,debit,credit)
    VALUES(p_tenant,p_credit,1,p_revenue,amount,0),
          (p_tenant,p_credit,2,p_ar,0,amount);
  PERFORM one_post_journal(p_tenant,p_credit,p_actor);
  INSERT INTO one_invoice_docs(
      tenant_id,id,kind,number,partner_id,issued_on,due_on,currency,
      source_invoice_id,control_account_id,journal_id,net_total,tax_total,gross_total)
    VALUES(p_tenant,p_credit,'customer_credit',btrim(p_number),original.partner_id,
      p_issued_on,p_issued_on,original.currency,p_original,p_ar,p_credit,
      amount,0,amount);
  INSERT INTO one_invoice_lines(
      tenant_id,doc_id,line_number,description,account_id,quantity,unit_price,
      discount_amount,tax_category,tax_code,tax_rate,input_tax_recoverable,
      base_total,tax_total)
    VALUES(p_tenant,p_credit,1,'Reviewed customer return',p_revenue,
      r.accepted_qty,original_line.unit_price,0,'out_of_scope','NONE',0,false,
      amount,0);
  PERFORM one_issue_invoice(p_tenant,p_credit,p_actor);
  PERFORM one_link_customer_return_credit(
      p_tenant,p_credit,p_original,p_return,p_actor);
  INSERT INTO one_nrv_customer_return_credit_links(
     tenant_id,return_job_id,original_invoice_id,credit_invoice_id,
     reviewed_nrv_event_id,approved_qty,issued_net,actor_id)
    VALUES(p_tenant,p_return,p_original,p_credit,review.new_nrv_event_id,
      r.accepted_qty,amount,p_actor);
  RETURN p_credit;
END $$;
REVOKE ALL ON one_nrv_customer_return_credit_links FROM PUBLIC;
REVOKE ALL ON FUNCTION one_issue_reviewed_nrv_customer_credit(
 uuid,uuid,uuid,uuid,text,uuid,text,text,uuid,date) FROM PUBLIC;
COMMIT;
