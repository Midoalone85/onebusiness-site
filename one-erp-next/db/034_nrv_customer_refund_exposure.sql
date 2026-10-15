-- ONE ERP NEXT / 034 | Read-only customer return settlement exposure.
-- This function does NOT approve a refund, cash payout, taxable note or GL.
-- Numbers are a point-in-time estimate; settlement must recheck under
-- original-invoice locks and verified identity in its OWN later transaction.
BEGIN;

CREATE FUNCTION one_nrv_return_settlement_exposure(
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
 IF current_status.internal_credit_posted THEN
  assessment='already_internally_credited';
 ELSIF gross>src.gross_total-credited OR claimed>r.accepted_qty THEN
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

REVOKE ALL ON FUNCTION one_nrv_return_settlement_exposure(
 uuid,uuid,uuid,uuid) FROM PUBLIC;
COMMIT;
