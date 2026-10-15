-- ONE ERP NEXT / 037 | Internal customer refund liability register.
-- Read-only accounting evidence, not legal invoicing or bank reconciliation.
BEGIN;
CREATE FUNCTION one_nrv_customer_refund_liability_register(
 p_tenant uuid,p_actor uuid,p_from date,p_until_exclusive date
) RETURNS TABLE(
 recognized_claims bigint,original_return_value numeric,
 reversed_receivables numeric,recognized_refund_liabilities numeric,
 unverifiable_claims bigint,accounting_reconciled boolean
) LANGUAGE plpgsql STABLE SECURITY INVOKER AS $$
BEGIN
 IF p_tenant IS NULL OR p_actor IS NULL OR p_from IS NULL
   OR p_until_exclusive IS NULL OR p_from>=p_until_exclusive
   OR current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text
   OR current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text
   OR NOT EXISTS(SELECT 1 FROM one_memberships m WHERE
     m.tenant_id=p_tenant AND m.actor_id=p_actor AND m.active
     AND m.role IN ('company_admin','accountant','viewer')) THEN
   RAISE EXCEPTION 'Authorized company refund liability report required';
 END IF;
 IF (SELECT count(DISTINCT j.currency)
    FROM one_nrv_refund_claims r
    JOIN one_journal_entries j ON j.tenant_id=r.tenant_id
      AND j.id=r.journal_id
    WHERE r.tenant_id=p_tenant AND j.occurred_on>=p_from
      AND j.occurred_on<p_until_exclusive)>1 THEN
    RAISE EXCEPTION 'Mixed-currency refund claims require functional conversion';
 END IF;
 RETURN QUERY
 SELECT count(*)::bigint,
   coalesce(sum(claim.gross_return_amount),0)::numeric,
   coalesce(sum(claim.ar_reduction),0)::numeric,
   coalesce(sum(claim.refund_liability),0)::numeric,
   count(*) FILTER(
     WHERE j.status IS DISTINCT FROM 'posted'
       OR j.currency IS DISTINCT FROM original.currency
       OR lines.total_debits IS DISTINCT FROM claim.gross_return_amount
       OR lines.total_credits IS DISTINCT FROM claim.gross_return_amount
       OR lines.sales_debit IS DISTINCT FROM claim.gross_return_amount
       OR lines.receivable_credit IS DISTINCT FROM claim.ar_reduction
       OR lines.refund_payable_credit IS DISTINCT FROM claim.refund_liability
       OR lines.line_count IS DISTINCT FROM
         (CASE WHEN claim.ar_reduction=0 THEN 2 ELSE 3 END)
       OR qa.source_reconciled IS DISTINCT FROM true
       OR qa.original_qty IS DISTINCT FROM claim.accepted_qty
    )::bigint,
   coalesce(bool_and(
      j.status='posted' AND j.currency=original.currency
      AND lines.total_debits=claim.gross_return_amount
      AND lines.total_credits=claim.gross_return_amount
      AND lines.sales_debit=claim.gross_return_amount
      AND lines.receivable_credit=claim.ar_reduction
      AND lines.refund_payable_credit=claim.refund_liability
      AND lines.line_count=(CASE WHEN claim.ar_reduction=0 THEN 2 ELSE 3 END)
      AND qa.source_reconciled AND qa.original_qty=claim.accepted_qty
   ),true)::boolean
 FROM one_nrv_refund_claims claim
 JOIN one_invoice_docs original ON original.tenant_id=claim.tenant_id
   AND original.id=claim.original_invoice_id
 JOIN one_journal_entries j ON j.tenant_id=claim.tenant_id
   AND j.id=claim.journal_id
 JOIN LATERAL one_nrv_qa_return_check(
   claim.tenant_id,p_actor,claim.return_job_id
 ) qa ON true
 CROSS JOIN LATERAL (
   SELECT count(*)::integer AS line_count,
     coalesce(sum(l.debit),0)::numeric AS total_debits,
     coalesce(sum(l.credit),0)::numeric AS total_credits,
     coalesce(sum(l.debit) FILTER (WHERE a.kind='revenue'),0)::numeric
        AS sales_debit,
     coalesce(sum(l.credit) FILTER (WHERE l.account_id=original.control_account_id),0)::numeric
        AS receivable_credit,
     coalesce(sum(l.credit) FILTER (WHERE l.account_id=claim.refund_account_id),0)::numeric
        AS refund_payable_credit
   FROM one_journal_lines l
   JOIN one_accounts a ON a.tenant_id=l.tenant_id AND a.id=l.account_id
   WHERE l.tenant_id=claim.tenant_id AND l.entry_id=claim.journal_id
 ) lines
 WHERE claim.tenant_id=p_tenant
   AND j.occurred_on>=p_from AND j.occurred_on<p_until_exclusive;
END $$;
REVOKE ALL ON FUNCTION one_nrv_customer_refund_liability_register(
 uuid,uuid,date,date) FROM PUBLIC;
COMMIT;
