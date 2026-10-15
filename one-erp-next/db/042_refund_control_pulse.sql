-- ONE ERP NEXT / 042 | ONE PULSE internal refund control summary.
-- Zero payout integration. All recognized liabilities stay OUTSTANDING until
-- future authenticated bank receipt and settlement accounting are implemented.
BEGIN;
CREATE FUNCTION one_refund_control_pulse(
 p_tenant uuid,p_actor uuid
) RETURNS TABLE (
 recognized_liability numeric,
 not_requested numeric,
 awaiting_independent_approval numeric,
 approved_without_bank_reference numeric,
 approved_with_unverified_reference numeric,
 accounting_control_discrepancies bigint,
 control_reconciled boolean,
 cash_state text
) LANGUAGE plpgsql STABLE SECURITY INVOKER AS $pulse_refunds$
BEGIN
 IF p_tenant IS NULL OR p_actor IS NULL OR
    current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
    current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
    NOT EXISTS(SELECT 1 FROM one_memberships m
      WHERE m.tenant_id=p_tenant AND m.actor_id=p_actor AND m.active
      AND m.role IN ('company_admin','accountant','viewer')) THEN
   RAISE EXCEPTION 'Tenant finance dashboard access required';
 END IF;
 IF (SELECT count(DISTINCT original.currency)
    FROM one_nrv_refund_claims c
    JOIN one_invoice_docs original ON original.tenant_id=c.tenant_id
      AND original.id=c.original_invoice_id
    WHERE c.tenant_id=p_tenant)>1 THEN
   RAISE EXCEPTION 'Multi-currency company refund dashboard requires IAS 21 conversion';
 END IF;
 RETURN QUERY
 SELECT
  coalesce(sum(c.refund_liability),0)::numeric,
  coalesce(sum(c.refund_liability) FILTER(WHERE o.id IS NULL),0)::numeric,
  coalesce(sum(c.refund_liability) FILTER(
     WHERE o.status='requested'),0)::numeric,
  coalesce(sum(c.refund_liability) FILTER(
     WHERE o.status='approved' AND b.id IS NULL),0)::numeric,
  coalesce(sum(c.refund_liability) FILTER(
     WHERE o.status='approved' AND b.id IS NOT NULL),0)::numeric,
  count(*) FILTER (
    WHERE j.status IS DISTINCT FROM 'posted' OR
     j.currency IS DISTINCT FROM original.currency OR
     o.amount IS DISTINCT FROM c.refund_liability AND o.id IS NOT NULL OR
     o.original_invoice_id IS DISTINCT FROM c.original_invoice_id
       AND o.id IS NOT NULL OR
     o.partner_id IS DISTINCT FROM original.partner_id AND o.id IS NOT NULL OR
     (o.status='approved' AND
       (o.approved_by IS NULL OR o.approved_by=o.requested_by OR
        o.approved_by=c.actor_id)) OR
     (b.id IS NOT NULL AND
       (o.status IS DISTINCT FROM 'approved' OR
        b.debit_amount IS DISTINCT FROM o.amount OR
        b.currency IS DISTINCT FROM o.currency OR
        b.beneficiary_evidence_ref IS DISTINCT FROM o.beneficiary_evidence_ref OR
        b.verification_status IS DISTINCT FROM 'unverified'))
  )::bigint,
  coalesce(bool_and(
     j.status='posted' AND j.currency=original.currency AND
     (o.id IS NULL OR (o.amount=c.refund_liability
       AND o.original_invoice_id=c.original_invoice_id
       AND o.partner_id=original.partner_id)) AND
     (o.status IS DISTINCT FROM 'approved' OR
       (o.approved_by IS NOT NULL AND o.approved_by<>o.requested_by
         AND o.approved_by<>c.actor_id)) AND
     (b.id IS NULL OR
       (o.status='approved' AND b.debit_amount=o.amount
         AND b.currency=o.currency
         AND b.beneficiary_evidence_ref=o.beneficiary_evidence_ref
         AND b.verification_status='unverified'))
    ),true)::boolean,
  'not_disbursed_no_authenticated_bank_feed'::text
 FROM one_nrv_refund_claims c
 JOIN one_invoice_docs original ON original.tenant_id=c.tenant_id
    AND original.id=c.original_invoice_id
 JOIN one_journal_entries j ON j.tenant_id=c.tenant_id
    AND j.id=c.journal_id
 LEFT JOIN one_customer_refund_orders o ON o.tenant_id=c.tenant_id
    AND o.return_job_id=c.return_job_id
 LEFT JOIN one_refund_bank_debit_candidates b ON b.tenant_id=o.tenant_id
    AND b.order_id=o.id
 WHERE c.tenant_id=p_tenant;
END $pulse_refunds$;
REVOKE ALL ON FUNCTION one_refund_control_pulse(uuid,uuid) FROM PUBLIC;
COMMIT;
