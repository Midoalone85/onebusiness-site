-- ONE ERP NEXT / 041 | Manually supplied bank debit candidate registry.
-- CAUTION: No authenticated bank feed and no real payout or GL settlement.
-- A matching statement reference is UNVERIFIED, never proof of payment.
BEGIN;
CREATE TABLE one_refund_bank_debit_candidates (
 tenant_id uuid NOT NULL,
 id uuid NOT NULL,
 order_id uuid NOT NULL,
 source_bank_account_ref text NOT NULL
     CHECK(length(btrim(source_bank_account_ref)) BETWEEN 8 AND 160),
 statement_import_ref text NOT NULL
     CHECK(length(btrim(statement_import_ref)) BETWEEN 8 AND 160),
 bank_transaction_ref text NOT NULL
     CHECK(length(btrim(bank_transaction_ref)) BETWEEN 8 AND 160),
 payment_reference text NOT NULL
     CHECK(length(btrim(payment_reference)) BETWEEN 36 AND 240),
 beneficiary_evidence_ref text NOT NULL,
 currency text NOT NULL REFERENCES one_currency_precision(code),
 debit_amount numeric(20,3) NOT NULL CHECK(debit_amount>0),
 value_date date NOT NULL,
 submitted_by uuid NOT NULL,
 submitted_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 verification_status text NOT NULL DEFAULT 'unverified'
     CHECK(verification_status='unverified'),
 PRIMARY KEY(tenant_id,id),
 UNIQUE(tenant_id,order_id),
 UNIQUE(tenant_id,source_bank_account_ref,statement_import_ref,
        bank_transaction_ref),
 FOREIGN KEY(tenant_id,order_id)
     REFERENCES one_customer_refund_orders(tenant_id,id)
);
ALTER TABLE one_refund_bank_debit_candidates ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_refund_bank_debit_candidates FORCE ROW LEVEL SECURITY;
CREATE POLICY one_refund_debit_candidate_scope ON one_refund_bank_debit_candidates
 USING(tenant_id::text=current_setting('one.tenant_id',true))
 WITH CHECK(tenant_id::text=current_setting('one.tenant_id',true));

CREATE FUNCTION one_refund_bank_candidate_immutable()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $bank_candidate_guard$
BEGIN
 IF TG_OP<>'INSERT' OR
    current_setting('one.refund_bank_candidate_context',true)
       IS DISTINCT FROM 'yes' OR
    NEW.verification_status<>'unverified' THEN
  RAISE EXCEPTION 'Bank candidate is immutable and unverified';
 END IF;
 RETURN NEW;
END $bank_candidate_guard$;
CREATE TRIGGER one_refund_bank_candidate_guard BEFORE INSERT OR UPDATE OR DELETE
 ON one_refund_bank_debit_candidates FOR EACH ROW
 EXECUTE FUNCTION one_refund_bank_candidate_immutable();

CREATE FUNCTION one_register_refund_bank_candidate(
 p_tenant uuid,p_order uuid,p_line uuid,p_actor uuid,
 p_bank_account_ref text,p_statement_ref text,p_transaction_ref text,
 p_payment_ref text,p_beneficiary_ref text,p_currency text,
 p_debit numeric,p_value_date date
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $refund_bank_register$
DECLARE
 ord one_customer_refund_orders%ROWTYPE;
 claim one_nrv_refund_claims%ROWTYPE;
 scale integer;
BEGIN
 IF p_tenant IS NULL OR p_order IS NULL OR p_line IS NULL OR
    p_actor IS NULL OR p_bank_account_ref IS NULL OR p_statement_ref IS NULL
    OR p_transaction_ref IS NULL OR p_payment_ref IS NULL
    OR p_beneficiary_ref IS NULL OR p_currency IS NULL OR
    p_debit IS NULL OR p_value_date IS NULL OR
    length(btrim(p_bank_account_ref)) NOT BETWEEN 8 AND 160 OR
    length(btrim(p_statement_ref)) NOT BETWEEN 8 AND 160 OR
    length(btrim(p_transaction_ref)) NOT BETWEEN 8 AND 160 OR
    length(btrim(p_payment_ref)) NOT BETWEEN 36 AND 240 OR
    current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
    current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
    NOT EXISTS(SELECT 1 FROM one_memberships m
       WHERE m.tenant_id=p_tenant AND m.actor_id=p_actor AND m.active
       AND m.role IN ('accountant','company_admin')) THEN
   RAISE EXCEPTION 'Scoped finance bank candidate preparer required';
 END IF;
 SELECT * INTO ord FROM one_customer_refund_orders
  WHERE tenant_id=p_tenant AND id=p_order FOR UPDATE;
 IF NOT FOUND OR ord.status<>'approved' OR
    ord.approved_by=p_actor OR ord.requested_by=p_actor OR
    p_value_date<ord.approved_at::date OR
    p_currency IS DISTINCT FROM ord.currency OR
    p_beneficiary_ref IS DISTINCT FROM ord.beneficiary_evidence_ref OR
    p_debit IS DISTINCT FROM ord.amount OR
    strpos(p_payment_ref,ord.id::text)=0 THEN
   RAISE EXCEPTION 'Independent bank candidate must match approved refund';
 END IF;
 SELECT * INTO claim FROM one_nrv_refund_claims
    WHERE tenant_id=p_tenant AND return_job_id=ord.return_job_id;
 IF NOT FOUND OR claim.actor_id=p_actor OR
    claim.refund_liability<>ord.amount OR
    claim.original_invoice_id<>ord.original_invoice_id THEN
   RAISE EXCEPTION 'Independent banking candidate source claim mismatch';
 END IF;
 IF p_currency<>'SAR' THEN
   RAISE EXCEPTION 'SAR-only candidate until FX rate pack is approved';
 END IF;
 SELECT minor_units INTO scale FROM one_currency_precision
   WHERE code=p_currency;
 IF scale<>2 OR p_debit<>round(p_debit,scale) THEN
   RAISE EXCEPTION 'Unsupported candidate debit currency precision';
 END IF;
 -- A submission proves only that a person entered data. Even a match
 -- remains UNVERIFIED: external bank ownership/signature is absent.
 PERFORM set_config('one.refund_bank_candidate_context','yes',true);
 INSERT INTO one_refund_bank_debit_candidates (
   tenant_id,id,order_id,source_bank_account_ref,statement_import_ref,
   bank_transaction_ref,payment_reference,beneficiary_evidence_ref,
   currency,debit_amount,value_date,submitted_by
 ) VALUES (
   p_tenant,p_line,p_order,btrim(p_bank_account_ref),btrim(p_statement_ref),
   btrim(p_transaction_ref),btrim(p_payment_ref),p_beneficiary_ref,
   p_currency,p_debit,p_value_date,p_actor
 );
 PERFORM set_config('one.refund_bank_candidate_context','',true);
 RETURN p_line;
END $refund_bank_register$;

CREATE FUNCTION one_refund_bank_candidate_company_overview(
 p_tenant uuid,p_actor uuid
) RETURNS TABLE(
 approved_unsettled_orders bigint,
 candidate_count bigint,
 candidate_amount numeric,
 approved_without_candidate bigint,
 unmatched_candidates bigint,
 bank_status text
) LANGUAGE plpgsql STABLE SECURITY INVOKER AS $refund_bank_report$
BEGIN
 IF p_tenant IS NULL OR p_actor IS NULL OR
    current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
    current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
    NOT EXISTS(SELECT 1 FROM one_memberships m
      WHERE m.tenant_id=p_tenant AND m.actor_id=p_actor AND m.active
      AND m.role IN ('accountant','company_admin','viewer')) THEN
  RAISE EXCEPTION 'Authorized company bank candidate reader required';
 END IF;
 IF (SELECT count(DISTINCT o.currency) FROM one_customer_refund_orders o
    WHERE o.tenant_id=p_tenant AND o.status='approved')>1 THEN
  RAISE EXCEPTION 'Mixed-currency bank reconciliation requires IAS 21 FX policy';
 END IF;
 RETURN QUERY
 SELECT
    (SELECT count(*) FROM one_customer_refund_orders o
      WHERE o.tenant_id=p_tenant AND o.status='approved')::bigint,
    count(c.id)::bigint,
    coalesce(sum(c.debit_amount),0)::numeric,
    (SELECT count(*) FROM one_customer_refund_orders o
      WHERE o.tenant_id=p_tenant AND o.status='approved'
        AND NOT EXISTS(SELECT 1 FROM one_refund_bank_debit_candidates b
          WHERE b.tenant_id=o.tenant_id AND b.order_id=o.id))::bigint,
    count(c.id) FILTER(WHERE
      o.id IS NULL OR o.status<>'approved' OR
      o.amount<>c.debit_amount OR o.currency<>c.currency OR
      o.beneficiary_evidence_ref<>c.beneficiary_evidence_ref OR
      strpos(c.payment_reference,c.order_id::text)=0)::bigint,
    'candidates_unverified_no_disbursement'::text
 FROM one_refund_bank_debit_candidates c
 LEFT JOIN one_customer_refund_orders o ON o.tenant_id=c.tenant_id
   AND o.id=c.order_id
 WHERE c.tenant_id=p_tenant;
END $refund_bank_report$;
REVOKE ALL ON one_refund_bank_debit_candidates FROM PUBLIC;
REVOKE ALL ON FUNCTION one_register_refund_bank_candidate(
 uuid,uuid,uuid,uuid,text,text,text,text,text,text,numeric,date)
 FROM PUBLIC;
REVOKE ALL ON FUNCTION one_refund_bank_candidate_company_overview(
 uuid,uuid) FROM PUBLIC;
COMMIT;
