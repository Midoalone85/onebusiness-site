-- ONE ERP NEXT / 036 | Recognize existing customer's return obligation.
-- Internal GL only: NO legal credit note, tax adjustment or bank disbursement.
BEGIN;
CREATE TABLE one_nrv_refund_claims (
 tenant_id uuid NOT NULL, return_job_id uuid NOT NULL,
 original_invoice_id uuid NOT NULL, journal_id uuid NOT NULL,
 accepted_qty numeric(20,3) NOT NULL CHECK(accepted_qty>0),
 gross_return_amount numeric(20,3) NOT NULL CHECK(gross_return_amount>0),
 ar_reduction numeric(20,3) NOT NULL CHECK(ar_reduction>=0),
 refund_liability numeric(20,3) NOT NULL CHECK(refund_liability>0),
 refund_account_id text NOT NULL,
 reason_reference text NOT NULL CHECK(length(btrim(reason_reference)) BETWEEN 5 AND 200),
 actor_id uuid NOT NULL, created_at timestamptz DEFAULT transaction_timestamp() NOT NULL,
 PRIMARY KEY(tenant_id,return_job_id), UNIQUE(tenant_id,journal_id),
 FOREIGN KEY(tenant_id,return_job_id)
   REFERENCES one_nrv_customer_return_reviews(tenant_id,return_job_id),
 FOREIGN KEY(tenant_id,original_invoice_id)
   REFERENCES one_invoice_docs(tenant_id,id),
 FOREIGN KEY(tenant_id,journal_id)
   REFERENCES one_journal_entries(tenant_id,id),
 FOREIGN KEY(tenant_id,refund_account_id)
   REFERENCES one_accounts(tenant_id,id),
 CHECK(gross_return_amount=ar_reduction+refund_liability)
);
ALTER TABLE one_nrv_refund_claims ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_nrv_refund_claims FORCE ROW LEVEL SECURITY;
CREATE POLICY nrv_refund_claim_scope ON one_nrv_refund_claims
 USING(tenant_id::text=current_setting('one.tenant_id',true))
 WITH CHECK(tenant_id::text=current_setting('one.tenant_id',true));
CREATE TRIGGER nrv_refund_claim_immutable BEFORE UPDATE OR DELETE
 ON one_nrv_refund_claims FOR EACH ROW EXECUTE FUNCTION one_protect_audit();

CREATE FUNCTION one_claim_block_ordinary_customer_credit() RETURNS trigger
 LANGUAGE plpgsql SECURITY INVOKER AS $$
BEGIN
 IF EXISTS(SELECT 1 FROM one_nrv_refund_claims c WHERE
 c.tenant_id=NEW.tenant_id AND c.return_job_id=NEW.return_job_id) THEN
  RAISE EXCEPTION 'Already recognized RMA claim cannot be credited twice';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER block_ordinary_credit_for_claim BEFORE INSERT
 ON one_customer_return_credits FOR EACH ROW
 EXECUTE FUNCTION one_claim_block_ordinary_customer_credit();

-- Prevent an independently issued credit from overrunning claims already
-- recognized on the same source invoice, even if no RMA link was supplied.
CREATE FUNCTION one_claim_protect_original_invoice() RETURNS trigger
 LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE claims numeric(24,3); previous numeric(24,3); original numeric(24,3);
BEGIN
 IF OLD.status='draft' AND NEW.status='issued' AND
    NEW.kind='customer_credit' AND NEW.source_invoice_id IS NOT NULL THEN
  SELECT gross_total INTO original FROM one_invoice_docs WHERE
   tenant_id=NEW.tenant_id AND id=NEW.source_invoice_id;
  SELECT coalesce(sum(gross_return_amount),0) INTO claims
    FROM one_nrv_refund_claims WHERE tenant_id=NEW.tenant_id
     AND original_invoice_id=NEW.source_invoice_id;
  SELECT coalesce(sum(gross_total),0) INTO previous
    FROM one_invoice_docs WHERE tenant_id=NEW.tenant_id
     AND source_invoice_id=NEW.source_invoice_id
     AND status='issued' AND kind='customer_credit';
  IF claims+previous>original THEN
    RAISE EXCEPTION 'Existing refund claim and issued credits exceed original invoice';
  END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER protect_original_from_credit_after_claim AFTER UPDATE
 ON one_invoice_docs FOR EACH ROW
 EXECUTE FUNCTION one_claim_protect_original_invoice();

CREATE FUNCTION one_recognize_nrv_customer_refund(
 p_tenant uuid,p_return uuid,p_original uuid,p_journal uuid,p_period uuid,
 p_ar text,p_revenue text,p_refund_due text,p_actor uuid,p_on date,
 p_reason text
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE
 src one_invoice_docs%ROWTYPE; r one_return_jobs%ROWTYPE;
 exposure record; prev_gross numeric(24,3); prev_ar numeric(24,3);
 prev_refund numeric(24,3); gross numeric(20,3);
 available_ar numeric(20,3); ar numeric(20,3); due numeric(20,3);
 line_no integer;
BEGIN
 IF p_tenant IS NULL OR p_return IS NULL OR p_original IS NULL
 OR p_journal IS NULL OR p_period IS NULL OR p_actor IS NULL
 OR p_on IS NULL OR p_reason IS NULL OR
 length(btrim(p_reason)) NOT BETWEEN 5 AND 200
 OR p_ar IS NULL OR p_revenue IS NULL OR p_refund_due IS NULL
 OR p_ar IN(p_revenue,p_refund_due) OR p_revenue=p_refund_due
 OR current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text
 OR current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text
 OR NOT EXISTS(SELECT 1 FROM one_memberships m WHERE
   m.tenant_id=p_tenant AND m.actor_id=p_actor AND m.active
   AND m.role IN ('company_admin','accountant')) THEN
  RAISE EXCEPTION 'Verified company accountant needed for refund liability';
 END IF;
 -- Same original invoice lock as customer receipts and source credits.
 SELECT * INTO src FROM one_invoice_docs
  WHERE tenant_id=p_tenant AND id=p_original FOR UPDATE;
 IF NOT FOUND OR src.status<>'issued' OR src.kind<>'customer_invoice'
    OR src.control_account_id<>p_ar OR src.tax_total<>0 OR
    src.issued_on>p_on THEN
  RAISE EXCEPTION 'Matching issued untaxed original invoice required';
 END IF;
 SELECT * INTO r FROM one_return_jobs
  WHERE tenant_id=p_tenant AND id=p_return FOR UPDATE;
 IF NOT FOUND OR r.status<>'completed' OR r.return_kind<>'customer'
    OR r.accepted_qty<=0 THEN
  RAISE EXCEPTION 'Accepted RMA required';
 END IF;
 IF EXISTS(SELECT 1 FROM one_nrv_refund_claims c
     WHERE c.tenant_id=p_tenant AND c.return_job_id=p_return)
    OR EXISTS(SELECT 1 FROM one_customer_return_credits c
     WHERE c.tenant_id=p_tenant AND c.return_job_id=p_return) THEN
  RAISE EXCEPTION 'RMA has already been recognized or credited';
 END IF;
 IF NOT EXISTS(SELECT 1 FROM one_accounts a WHERE a.tenant_id=p_tenant
     AND a.id=p_ar AND a.kind='asset' AND a.active AND a.posting_allowed)
    OR NOT EXISTS(SELECT 1 FROM one_accounts a WHERE a.tenant_id=p_tenant
     AND a.id=p_revenue AND a.kind='revenue' AND a.active AND a.posting_allowed)
    OR NOT EXISTS(SELECT 1 FROM one_accounts a WHERE a.tenant_id=p_tenant
     AND a.id=p_refund_due AND a.kind='liability'
     AND a.active AND a.posting_allowed) THEN
  RAISE EXCEPTION 'Valid original AR/revenue and refund liability required';
 END IF;
 SELECT * INTO exposure FROM one_nrv_return_settlement_exposure(
   p_tenant,p_actor,p_return,p_original);
 IF NOT FOUND OR exposure.settlement_state NOT IN
  ('requires_refund_liability_workflow','internal_credit_ready')
   OR exposure.estimated_credit_vat<>0
   OR exposure.estimated_credit_gross<=0 THEN
  RAISE EXCEPTION 'Unreconciled or taxable refund; refer to country tax pack';
 END IF;
 SELECT coalesce(sum(gross_return_amount),0),coalesce(sum(ar_reduction),0),
        coalesce(sum(refund_liability),0)
  INTO prev_gross,prev_ar,prev_refund FROM one_nrv_refund_claims
  WHERE tenant_id=p_tenant AND original_invoice_id=p_original;
 gross=exposure.estimated_credit_gross;
 available_ar=exposure.open_receivable;
 IF available_ar<0 OR
    prev_gross+gross+exposure.issued_customer_credits >
       exposure.original_invoice_gross THEN
   RAISE EXCEPTION 'Original invoice financial ceiling exhausted';
 END IF;
 ar=least(gross,available_ar);
 due=gross-ar;
 IF due<=0 OR due+prev_refund>exposure.applied_customer_receipts
   OR ar>exposure.open_receivable THEN
   RAISE EXCEPTION 'No paid amount available for backed refund liability';
 END IF;
 INSERT INTO one_journal_entries
  (tenant_id,id,period_id,reference,idempotency_key,occurred_on,currency)
 VALUES(p_tenant,p_journal,p_period,'refund-liability:'||p_return::text,
   'refund-liability:'||p_return::text,p_on,src.currency);
 INSERT INTO one_journal_lines
  (tenant_id,entry_id,line_number,account_id,debit,credit)
 VALUES(p_tenant,p_journal,1,p_revenue,gross,0);
 line_no=2;
 IF ar>0 THEN
  INSERT INTO one_journal_lines
   (tenant_id,entry_id,line_number,account_id,debit,credit)
  VALUES(p_tenant,p_journal,line_no,p_ar,0,ar);
  line_no=line_no+1;
 END IF;
 INSERT INTO one_journal_lines
  (tenant_id,entry_id,line_number,account_id,debit,credit)
 VALUES(p_tenant,p_journal,line_no,p_refund_due,0,due);
 PERFORM one_post_journal(p_tenant,p_journal,p_actor);
 INSERT INTO one_nrv_refund_claims(
   tenant_id,return_job_id,original_invoice_id,journal_id,accepted_qty,
   gross_return_amount,ar_reduction,refund_liability,refund_account_id,
   reason_reference,actor_id)
 VALUES(p_tenant,p_return,p_original,p_journal,r.accepted_qty,gross,
    ar,due,p_refund_due,btrim(p_reason),p_actor);
 RETURN p_journal;
END $$;
REVOKE ALL ON one_nrv_refund_claims FROM PUBLIC;
REVOKE ALL ON FUNCTION one_recognize_nrv_customer_refund(
 uuid,uuid,uuid,uuid,uuid,text,text,text,uuid,date,text) FROM PUBLIC;

-- Read model 034 now includes recognized internal claims and refuses
-- to propose the same refund after recognition.
CREATE OR REPLACE FUNCTION one_nrv_return_settlement_exposure(
 p_tenant uuid,p_actor uuid,p_return uuid,p_original uuid
) RETURNS TABLE(
 original_invoice_id uuid,accepted_qty numeric,original_invoice_gross numeric,
 applied_customer_receipts numeric,issued_customer_credits numeric,
 open_receivable numeric,estimated_credit_base numeric,
 estimated_credit_vat numeric,estimated_credit_gross numeric,
 proposed_ar_reduction numeric,proposed_refund_liability numeric,
 settlement_state text
) LANGUAGE plpgsql STABLE SECURITY INVOKER AS $$
DECLARE
 src one_invoice_docs%ROWTYPE;
 r one_return_jobs%ROWTYPE;
 mov one_stock_events%ROWTYPE;
 l one_invoice_lines%ROWTYPE;
 alloc one_sales_invoice_allocations%ROWTYPE;
 current_status record;
 paid numeric(24,3);
 credited numeric(24,3);
 open_ar numeric(24,3);
 base numeric(24,3);
 tax numeric(24,3);
 gross numeric(24,3);
 claimed numeric(24,3);
 claims_total numeric(24,3);
 claims_ar numeric(24,3);
 has_claim boolean;
 minor integer;
 assessment text;
BEGIN
 IF p_tenant IS NULL OR p_actor IS NULL OR p_return IS NULL OR
    p_original IS NULL OR
    current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
    current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
    NOT EXISTS(SELECT 1 FROM one_memberships m WHERE m.tenant_id=p_tenant
      AND m.actor_id=p_actor AND m.active
      AND m.role IN ('company_admin','accountant','viewer')) THEN
  RAISE EXCEPTION 'Authorized tenant finance reader required';
 END IF;
 SELECT * INTO r FROM one_return_jobs
   WHERE tenant_id=p_tenant AND id=p_return;
 IF NOT FOUND OR r.return_kind<>'customer' OR r.status<>'completed' OR
    r.accepted_qty IS NULL OR r.accepted_qty<=0 THEN
  RAISE EXCEPTION 'A completed accepted RMA required';
 END IF;
 SELECT * INTO current_status FROM one_nrv_customer_credit_case(
   p_tenant,p_actor,p_return);
 IF NOT FOUND OR current_status.qa_reconciled IS DISTINCT FROM true OR
   current_status.accepted_qty<>r.accepted_qty THEN
  RAISE EXCEPTION 'Return QA evidence and accounting must reconcile';
 END IF;
 SELECT * INTO mov FROM one_stock_events
    WHERE tenant_id=p_tenant AND id=r.source_event_id;
 IF NOT FOUND OR mov.movement_kind<>'sale_dispatch' THEN
  RAISE EXCEPTION 'Original dispatched customer source required';
 END IF;
 SELECT * INTO src FROM one_invoice_docs
   WHERE tenant_id=p_tenant AND id=p_original;
 IF NOT FOUND OR src.kind<>'customer_invoice' OR src.status<>'issued'
    OR src.partner_id IS DISTINCT FROM (
       SELECT partner_id FROM one_commercial_orders
       WHERE tenant_id=p_tenant AND id=mov.order_id) THEN
  RAISE EXCEPTION 'Issued matching customer invoice required';
 END IF;
 IF (SELECT count(*) FROM one_invoice_lines
      WHERE tenant_id=p_tenant AND doc_id=p_original)<>1 THEN
  RAISE EXCEPTION 'Only single-line source invoice supported';
 END IF;
 SELECT * INTO alloc FROM one_sales_invoice_allocations
  WHERE tenant_id=p_tenant AND invoice_id=p_original
    AND order_id=mov.order_id AND order_line_no=mov.line_no;
 IF NOT FOUND OR alloc.invoice_line_number<>1 OR
    alloc.billed_qty<r.accepted_qty THEN
  RAISE EXCEPTION 'Accepted return does not match original billed stock';
 END IF;
 SELECT * INTO l FROM one_invoice_lines
   WHERE tenant_id=p_tenant AND doc_id=p_original AND line_number=1;
 IF NOT FOUND OR l.quantity<>alloc.billed_qty OR l.unit_price<=0 OR
    l.discount_amount<>0 OR l.input_tax_recoverable OR
    l.quantity<>round(l.quantity,3) THEN
  RAISE EXCEPTION 'Unsupported mismatched/discounted source line';
 END IF;
 SELECT minor_units INTO minor FROM one_currency_precision
   WHERE code=src.currency;
 IF minor IS NULL OR l.unit_price<>round(l.unit_price,minor) THEN
   RAISE EXCEPTION 'Source invoice precision unavailable';
 END IF;
 SELECT coalesce(sum(p.amount),0) INTO paid
  FROM one_payment_allocations p
  JOIN one_payment_docs d ON d.tenant_id=p.tenant_id
    AND d.id=p.payment_id AND d.status='applied'
  WHERE p.tenant_id=p_tenant AND p.invoice_id=p_original
    AND d.direction='customer_receipt';
 SELECT coalesce(sum(d.gross_total),0) INTO credited
  FROM one_invoice_docs d WHERE d.tenant_id=p_tenant
    AND d.source_invoice_id=p_original AND d.status='issued'
    AND d.kind='customer_credit';
 IF paid<0 OR credited<0 OR paid+credited>src.gross_total THEN
   RAISE EXCEPTION 'Existing paid/credited financial source does not reconcile';
 END IF;
 open_ar=src.gross_total-paid-credited;
 base=round(r.accepted_qty*l.unit_price,minor);
 -- For rated invoices this is *an estimate only*. The live credit tax base,
 -- tax-point, legal note, and VAT period MUST be determined by KSA tax pack.
 tax=round(base*l.tax_rate,minor);
 gross=base+tax;
 SELECT coalesce(sum(c.quantity),0) INTO claimed
  FROM one_customer_return_credits c
  WHERE c.tenant_id=p_tenant AND c.return_job_id=p_return;
 SELECT coalesce(sum(gross_return_amount),0),
         coalesce(sum(ar_reduction),0)
   INTO claims_total,claims_ar FROM one_nrv_refund_claims
   WHERE tenant_id=p_tenant AND original_invoice_id=p_original;
 SELECT EXISTS(SELECT 1 FROM one_nrv_refund_claims
   WHERE tenant_id=p_tenant AND return_job_id=p_return) INTO has_claim;
 IF claims_ar>open_ar OR claims_total+credited>src.gross_total THEN
   RAISE EXCEPTION 'Existing refund commitments do not reconcile';
 END IF;
 open_ar=open_ar-claims_ar;
 IF has_claim THEN
  assessment='refund_liability_recognized';
 ELSIF current_status.internal_credit_posted THEN
  assessment='already_internally_credited';
 ELSIF gross>src.gross_total-credited-claims_total OR claimed>r.accepted_qty THEN
  assessment='credit_ceiling_review';
 ELSIF current_status.case_status<>'awaiting_internal_credit' THEN
  assessment='financial_discrepancy';
 ELSIF src.tax_total<>0 OR l.tax_rate<>0 OR
     l.tax_category<>'out_of_scope' OR l.tax_code<>'NONE' THEN
  assessment='requires_zatca_tax_pack';
 ELSIF gross>open_ar THEN
  assessment='requires_refund_liability_workflow';
 ELSE
  assessment='internal_credit_ready';
 END IF;

 RETURN QUERY SELECT p_original,r.accepted_qty,src.gross_total,
  paid,credited,open_ar,base,tax,gross,
  CASE WHEN assessment IN (
      'internal_credit_ready','requires_refund_liability_workflow',
      'requires_zatca_tax_pack')
    THEN least(gross,open_ar) ELSE 0 END,
  CASE WHEN assessment IN (
      'internal_credit_ready','requires_refund_liability_workflow',
      'requires_zatca_tax_pack')
    THEN greatest(gross-open_ar,0) ELSE 0 END,
  assessment;
END $$;


COMMIT;
