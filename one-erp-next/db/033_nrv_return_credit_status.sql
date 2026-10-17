-- ONE ERP NEXT / 033 | Internal reviewed RMA financial readiness report.
-- Read only, no payment, refund, ZATCA legal note or account mutation.
BEGIN;
CREATE FUNCTION one_nrv_customer_credit_case(
 p_tenant uuid,p_actor uuid,p_return uuid
) RETURNS TABLE(
 accepted_qty numeric,
 qa_gross_cost numeric,
 qa_nrv_allowance numeric,
 qa_net_carrying numeric,
 qa_reconciled boolean,
 credit_invoice_id uuid,
 original_invoice_id uuid,
 credited_qty numeric,
 credited_amount numeric,
 internal_credit_posted boolean,
 case_status text
) LANGUAGE plpgsql STABLE SECURITY INVOKER AS $$
BEGIN
 IF p_tenant IS NULL OR p_actor IS NULL OR p_return IS NULL OR
   current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
   current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
   NOT EXISTS(SELECT 1 FROM one_memberships m
    WHERE m.tenant_id=p_tenant AND m.actor_id=p_actor AND m.active
      AND m.role IN ('company_admin','accountant','viewer')) THEN
   RAISE EXCEPTION 'Authorized company return credit status required';
 END IF;
 IF NOT EXISTS(SELECT 1 FROM one_return_jobs j WHERE j.tenant_id=p_tenant
    AND j.id=p_return) THEN
   RAISE EXCEPTION 'Unknown or inaccessible RMA';
 END IF;
 RETURN QUERY
 SELECT r.accepted_qty, q.original_cost, q.new_allowance,q.qa_net_value,
   q.source_reconciled,lk.credit_invoice_id,lk.original_invoice_id,
   coalesce(lk.approved_qty,0)::numeric,
   coalesce(lk.issued_net,0)::numeric,
   (lk.credit_invoice_id IS NOT NULL
       AND d.status='issued' AND d.kind='customer_credit'
       AND d.source_invoice_id=lk.original_invoice_id
       AND d.net_total=lk.issued_net AND d.tax_total=0
       AND j.status='posted' AND j.id=d.journal_id
       AND j.tenant_id=p_tenant
       AND linked.credit_invoice_id=lk.credit_invoice_id
       AND linked.quantity=lk.approved_qty
       AND lk.approved_qty=r.accepted_qty
       AND lk.issued_net=d.gross_total)::boolean,
   (CASE
      WHEN q.source_reconciled IS DISTINCT FROM true THEN 'qa_discrepancy'
      WHEN lk.credit_invoice_id IS NULL THEN 'awaiting_internal_credit'
      WHEN d.status='issued' AND d.kind='customer_credit'
       AND d.source_invoice_id=lk.original_invoice_id
       AND d.net_total=lk.issued_net AND d.tax_total=0
       AND j.status='posted' AND j.id=d.journal_id
       AND j.tenant_id=p_tenant AND
       linked.credit_invoice_id=lk.credit_invoice_id
       AND linked.quantity=lk.approved_qty
       AND lk.approved_qty=r.accepted_qty
       AND lk.issued_net=d.gross_total
         THEN 'internal_credit_posted'
      ELSE 'financial_discrepancy'
    END)::text
 FROM one_return_jobs r
 JOIN one_nrv_customer_return_reviews review
   ON review.tenant_id=r.tenant_id AND review.return_job_id=r.id
 JOIN LATERAL one_nrv_qa_return_check(p_tenant,p_actor,p_return) q
   ON true
 LEFT JOIN one_nrv_customer_return_credit_links lk
   ON lk.tenant_id=r.tenant_id AND lk.return_job_id=r.id
 LEFT JOIN one_invoice_docs d
   ON d.tenant_id=lk.tenant_id AND d.id=lk.credit_invoice_id
 LEFT JOIN one_journal_entries j
   ON j.tenant_id=d.tenant_id AND j.id=d.journal_id
 LEFT JOIN one_customer_return_credits linked
   ON linked.tenant_id=lk.tenant_id
   AND linked.credit_invoice_id=lk.credit_invoice_id
   AND linked.return_job_id=r.id
 WHERE r.tenant_id=p_tenant AND r.id=p_return;
END $$;
REVOKE ALL ON FUNCTION one_nrv_customer_credit_case(uuid,uuid,uuid) FROM PUBLIC;
COMMIT;
