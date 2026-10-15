-- ONE ERP NEXT / 039 | Finance review of approved internal refund instructions.
-- Read only. Approval is not settlement. NO external bank or ledger posting.
BEGIN;
CREATE FUNCTION one_customer_refund_approval_overview(
 p_tenant uuid,p_actor uuid,p_from date,p_until_exclusive date
) RETURNS TABLE(
 requested_orders bigint,approved_orders bigint,
 pending_review_amount numeric,approved_not_paid_amount numeric,
 unapplied_claims bigint,inconsistent_orders bigint,
 internal_review_reconciled boolean
) LANGUAGE plpgsql STABLE SECURITY INVOKER AS $refund_overview$
BEGIN
 IF p_tenant IS NULL OR p_actor IS NULL OR p_from IS NULL
    OR p_until_exclusive IS NULL OR p_from>=p_until_exclusive
    OR current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text
    OR current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text
    OR NOT EXISTS(SELECT 1 FROM one_memberships m
       WHERE m.tenant_id=p_tenant AND m.actor_id=p_actor AND m.active
       AND m.role IN ('company_admin','accountant','viewer')) THEN
   RAISE EXCEPTION 'Approved tenant financial reader required';
 END IF;
 IF (SELECT count(DISTINCT o.currency)
    FROM one_customer_refund_orders o WHERE o.tenant_id=p_tenant
    AND o.requested_at::date>=p_from
    AND o.requested_at::date<p_until_exclusive)>1 THEN
   RAISE EXCEPTION 'Mixed-currency customer refund exposure needs IAS 21 conversion';
 END IF;
 RETURN QUERY
 SELECT count(*) FILTER(WHERE o.status='requested')::bigint,
   count(*) FILTER(WHERE o.status='approved')::bigint,
   coalesce(sum(o.amount) FILTER(WHERE o.status='requested'),0)::numeric,
   coalesce(sum(o.amount) FILTER(WHERE o.status='approved'),0)::numeric,
   (
    SELECT count(*) FROM one_nrv_refund_claims u
    JOIN one_journal_entries j ON j.tenant_id=u.tenant_id AND j.id=u.journal_id
    WHERE u.tenant_id=p_tenant
      AND j.occurred_on>=p_from AND j.occurred_on<p_until_exclusive
      AND NOT EXISTS(SELECT 1 FROM one_customer_refund_orders x
         WHERE x.tenant_id=u.tenant_id AND x.return_job_id=u.return_job_id)
   )::bigint,
   count(*) FILTER(WHERE
     c.return_job_id IS NULL OR
     c.refund_liability IS DISTINCT FROM o.amount OR
     c.original_invoice_id IS DISTINCT FROM o.original_invoice_id OR
     source.id IS NULL OR source.status IS DISTINCT FROM 'posted' OR
     source.currency IS DISTINCT FROM o.currency OR
     invoice.id IS NULL OR invoice.partner_id IS DISTINCT FROM o.partner_id OR
     requested.event IS NULL OR requested.actor_id IS DISTINCT FROM o.requested_by OR
     (o.status='approved' AND
       (approved.event IS NULL OR
        approved.actor_id IS DISTINCT FROM o.approved_by OR
        o.approved_by IS NOT DISTINCT FROM o.requested_by OR
        o.approved_by IS NOT DISTINCT FROM c.actor_id))
   )::bigint,
   coalesce(bool_and(
     c.return_job_id IS NOT NULL AND
     c.refund_liability=o.amount AND
     c.original_invoice_id=o.original_invoice_id AND
     source.id IS NOT NULL AND source.status='posted' AND
     source.currency=o.currency AND invoice.id IS NOT NULL AND
     invoice.partner_id=o.partner_id AND
     requested.event IS NOT NULL AND requested.actor_id=o.requested_by AND
     (o.status='requested' OR
      (approved.event IS NOT NULL AND approved.actor_id=o.approved_by AND
       o.approved_by<>o.requested_by AND o.approved_by<>c.actor_id))
   ),true)::boolean
 FROM one_customer_refund_orders o
 LEFT JOIN one_nrv_refund_claims c ON c.tenant_id=o.tenant_id
   AND c.return_job_id=o.return_job_id
 LEFT JOIN one_journal_entries source ON source.tenant_id=c.tenant_id
   AND source.id=c.journal_id
 LEFT JOIN one_invoice_docs invoice ON invoice.tenant_id=o.tenant_id
   AND invoice.id=o.original_invoice_id
 LEFT JOIN one_customer_refund_approval_audit requested
   ON requested.tenant_id=o.tenant_id AND requested.order_id=o.id
   AND requested.event='refund.requested'
 LEFT JOIN one_customer_refund_approval_audit approved
   ON approved.tenant_id=o.tenant_id AND approved.order_id=o.id
   AND approved.event='refund.approved'
 WHERE o.tenant_id=p_tenant
    AND o.requested_at::date>=p_from AND o.requested_at::date<p_until_exclusive;
END $refund_overview$;

-- One authorized detail. Do not emit a bank account number, do not call
-- external provider. The evidence reference is only an internal pointer.
CREATE FUNCTION one_refund_approved_instruction_preview(
 p_tenant uuid,p_actor uuid,p_order uuid
) RETURNS TABLE(
 refund_order_id uuid, original_invoice_id uuid, partner_id text,
 currency text, amount numeric, evidence_reference text,
 approval_reference text, financial_posting_status text
) LANGUAGE plpgsql STABLE SECURITY INVOKER AS $approved_preview$
BEGIN
 IF p_tenant IS NULL OR p_actor IS NULL OR p_order IS NULL OR
    current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
    current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
    NOT EXISTS(SELECT 1 FROM one_memberships m WHERE
       m.tenant_id=p_tenant AND m.actor_id=p_actor AND m.active
       AND m.role IN ('company_admin','accountant')) THEN
  RAISE EXCEPTION 'Approved tenant finance preparer required';
 END IF;
 RETURN QUERY
 SELECT o.id,o.original_invoice_id,o.partner_id,o.currency,o.amount,
   o.beneficiary_evidence_ref,o.approval_evidence_ref,
   'approved_not_disbursed'::text
 FROM one_customer_refund_orders o
 JOIN one_nrv_refund_claims c ON c.tenant_id=o.tenant_id
   AND c.return_job_id=o.return_job_id
 JOIN one_journal_entries j ON j.tenant_id=c.tenant_id AND j.id=c.journal_id
 WHERE o.tenant_id=p_tenant AND o.id=p_order AND o.status='approved'
   AND j.status='posted' AND c.refund_liability=o.amount
   AND c.original_invoice_id=o.original_invoice_id;
END $approved_preview$;
REVOKE ALL ON FUNCTION one_customer_refund_approval_overview(
 uuid,uuid,date,date) FROM PUBLIC;
REVOKE ALL ON FUNCTION one_refund_approved_instruction_preview(
 uuid,uuid,uuid) FROM PUBLIC;
COMMIT;
