-- ONE ERP NEXT / 038 | Controlled internal refund payment approval.
-- No bank account details, beneficiary verification, payment instruction
-- to a provider, ZATCA document or movement of cash occurs in this module.
BEGIN;

CREATE TABLE one_customer_refund_orders (
 tenant_id uuid NOT NULL,
 id uuid NOT NULL,
 return_job_id uuid NOT NULL,
 original_invoice_id uuid NOT NULL,
 partner_id text NOT NULL,
 currency text NOT NULL REFERENCES one_currency_precision(code),
 amount numeric(20,3) NOT NULL CHECK(amount>0),
 beneficiary_evidence_ref text NOT NULL CHECK(length(btrim(beneficiary_evidence_ref)) BETWEEN 8 AND 160),
 request_reason text NOT NULL CHECK(length(btrim(request_reason)) BETWEEN 8 AND 240),
 requested_by uuid NOT NULL,
 requested_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 status text NOT NULL DEFAULT 'requested'
    CHECK(status IN ('requested','approved')),
 approved_by uuid,
 approval_evidence_ref text,
 approved_at timestamptz,
 PRIMARY KEY(tenant_id,id),
 UNIQUE(tenant_id,return_job_id),
 FOREIGN KEY(tenant_id,return_job_id)
    REFERENCES one_nrv_refund_claims(tenant_id,return_job_id),
 FOREIGN KEY(tenant_id,original_invoice_id)
    REFERENCES one_invoice_docs(tenant_id,id),
 CHECK ((status='requested' AND approved_by IS NULL AND
         approved_at IS NULL AND approval_evidence_ref IS NULL)
    OR (status='approved' AND approved_by IS NOT NULL AND
         approved_at IS NOT NULL AND approval_evidence_ref IS NOT NULL
         AND length(btrim(approval_evidence_ref)) BETWEEN 8 AND 160
         AND approved_by<>requested_by))
);
CREATE TABLE one_customer_refund_approval_audit (
 tenant_id uuid NOT NULL,
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 order_id uuid NOT NULL,
 event text NOT NULL CHECK(event IN ('refund.requested','refund.approved')),
 actor_id uuid NOT NULL,
 occurred_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 UNIQUE(tenant_id,order_id,event),
 FOREIGN KEY(tenant_id,order_id)
    REFERENCES one_customer_refund_orders(tenant_id,id)
);

ALTER TABLE one_customer_refund_orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_customer_refund_orders FORCE ROW LEVEL SECURITY;
CREATE POLICY one_refund_order_scope ON one_customer_refund_orders
 USING(tenant_id::text=current_setting('one.tenant_id',true))
 WITH CHECK(tenant_id::text=current_setting('one.tenant_id',true));
ALTER TABLE one_customer_refund_approval_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_customer_refund_approval_audit FORCE ROW LEVEL SECURITY;
CREATE POLICY one_refund_approval_scope ON one_customer_refund_approval_audit
 USING(tenant_id::text=current_setting('one.tenant_id',true))
 WITH CHECK(tenant_id::text=current_setting('one.tenant_id',true));

CREATE FUNCTION one_refund_order_write_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER AS $refund_order_guard$
BEGIN
 IF TG_OP='DELETE' OR
    current_setting('one.refund_order_context',true) IS DISTINCT FROM 'yes' THEN
  RAISE EXCEPTION 'Refund orders can only change through authorized workflow';
 END IF;
 IF TG_OP='INSERT' THEN
  IF NEW.status<>'requested' OR NEW.approved_by IS NOT NULL THEN
   RAISE EXCEPTION 'New refund order must be requested';
  END IF;
 ELSE
  IF OLD.status<>'requested' OR NEW.status<>'approved' OR
     ROW(NEW.tenant_id,NEW.id,NEW.return_job_id,NEW.original_invoice_id,
         NEW.partner_id,NEW.currency,NEW.amount,NEW.beneficiary_evidence_ref,
         NEW.request_reason,NEW.requested_by,NEW.requested_at) IS DISTINCT FROM
     ROW(OLD.tenant_id,OLD.id,OLD.return_job_id,OLD.original_invoice_id,
         OLD.partner_id,OLD.currency,OLD.amount,OLD.beneficiary_evidence_ref,
         OLD.request_reason,OLD.requested_by,OLD.requested_at) THEN
   RAISE EXCEPTION 'Refund request data is immutable after submission';
  END IF;
 END IF;
 RETURN NEW;
END $refund_order_guard$;
CREATE TRIGGER one_refund_order_guard BEFORE INSERT OR UPDATE OR DELETE
 ON one_customer_refund_orders FOR EACH ROW
 EXECUTE FUNCTION one_refund_order_write_guard();
CREATE TRIGGER one_refund_approval_audit_immutable BEFORE UPDATE OR DELETE
 ON one_customer_refund_approval_audit FOR EACH ROW
 EXECUTE FUNCTION one_protect_audit();

CREATE FUNCTION one_request_customer_refund_order(
 p_tenant uuid,p_return uuid,p_order uuid,p_actor uuid,
 p_beneficiary_evidence text,p_reason text
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $request_refund$
DECLARE claim one_nrv_refund_claims%ROWTYPE;
 original one_invoice_docs%ROWTYPE;
 source_journal one_journal_entries%ROWTYPE;
BEGIN
 IF p_tenant IS NULL OR p_return IS NULL OR p_order IS NULL OR
    p_actor IS NULL OR p_beneficiary_evidence IS NULL OR
    p_reason IS NULL OR
    length(btrim(p_beneficiary_evidence)) NOT BETWEEN 8 AND 160 OR
    length(btrim(p_reason)) NOT BETWEEN 8 AND 240 OR
    current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
    current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
    NOT EXISTS(SELECT 1 FROM one_memberships m WHERE
      m.tenant_id=p_tenant AND m.actor_id=p_actor AND m.active
      AND m.role IN ('accountant','company_admin')) THEN
  RAISE EXCEPTION 'Authorized finance refund preparer and evidence required';
 END IF;
 SELECT * INTO claim FROM one_nrv_refund_claims
  WHERE tenant_id=p_tenant AND return_job_id=p_return FOR UPDATE;
 IF NOT FOUND OR claim.refund_liability<=0 OR
    claim.ar_reduction+claim.refund_liability<>claim.gross_return_amount THEN
  RAISE EXCEPTION 'Recognized unresolved customer refund liability needed';
 END IF;
 IF EXISTS(SELECT 1 FROM one_customer_refund_orders r
    WHERE r.tenant_id=p_tenant AND r.return_job_id=p_return) THEN
  RAISE EXCEPTION 'Return already has an internal refund instruction';
 END IF;
 SELECT * INTO original FROM one_invoice_docs
   WHERE tenant_id=p_tenant AND id=claim.original_invoice_id;
 SELECT * INTO source_journal FROM one_journal_entries
   WHERE tenant_id=p_tenant AND id=claim.journal_id;
 IF original.id IS NULL OR original.kind<>'customer_invoice'
    OR original.status<>'issued' OR source_journal.id IS NULL
    OR source_journal.status<>'posted'
    OR source_journal.currency<>original.currency THEN
  RAISE EXCEPTION 'Original invoice and source refund GL do not reconcile';
 END IF;
 PERFORM set_config('one.refund_order_context','yes',true);
 INSERT INTO one_customer_refund_orders(
   tenant_id,id,return_job_id,original_invoice_id,partner_id,currency,
   amount,beneficiary_evidence_ref,request_reason,requested_by)
 VALUES(p_tenant,p_order,p_return,claim.original_invoice_id,original.partner_id,
   original.currency,claim.refund_liability,btrim(p_beneficiary_evidence),
   btrim(p_reason),p_actor);
 PERFORM set_config('one.refund_order_context','',true);
 INSERT INTO one_customer_refund_approval_audit(
   tenant_id,order_id,event,actor_id)
 VALUES(p_tenant,p_order,'refund.requested',p_actor);
 RETURN p_order;
END $request_refund$;

CREATE FUNCTION one_approve_customer_refund_order(
 p_tenant uuid,p_order uuid,p_actor uuid,p_approval_evidence text
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $approve_refund$
DECLARE ord one_customer_refund_orders%ROWTYPE;
 claim one_nrv_refund_claims%ROWTYPE;
BEGIN
 IF p_tenant IS NULL OR p_order IS NULL OR p_actor IS NULL OR
    p_approval_evidence IS NULL OR
    length(btrim(p_approval_evidence)) NOT BETWEEN 8 AND 160 OR
    current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
    current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
    NOT EXISTS(SELECT 1 FROM one_memberships m WHERE
      m.tenant_id=p_tenant AND m.actor_id=p_actor AND m.active
      AND m.role='company_admin') THEN
  RAISE EXCEPTION 'Independent company administrator approval required';
 END IF;
 SELECT * INTO ord FROM one_customer_refund_orders
   WHERE tenant_id=p_tenant AND id=p_order FOR UPDATE;
 IF NOT FOUND OR ord.status<>'requested' OR ord.requested_by=p_actor THEN
  RAISE EXCEPTION 'Different preparer and approving actor required';
 END IF;
 SELECT * INTO claim FROM one_nrv_refund_claims
   WHERE tenant_id=p_tenant AND return_job_id=ord.return_job_id FOR UPDATE;
 IF claim.return_job_id IS NULL OR claim.actor_id=p_actor OR
    claim.refund_liability<>ord.amount OR
    claim.original_invoice_id<>ord.original_invoice_id THEN
  RAISE EXCEPTION 'Approver independent from claim maker, and unchanged value required';
 END IF;
 PERFORM set_config('one.refund_order_context','yes',true);
 UPDATE one_customer_refund_orders
 SET status='approved',approved_by=p_actor,approved_at=transaction_timestamp(),
     approval_evidence_ref=btrim(p_approval_evidence)
 WHERE tenant_id=p_tenant AND id=p_order;
 PERFORM set_config('one.refund_order_context','',true);
 INSERT INTO one_customer_refund_approval_audit(
   tenant_id,order_id,event,actor_id)
 VALUES(p_tenant,p_order,'refund.approved',p_actor);
 RETURN p_order;
END $approve_refund$;
REVOKE ALL ON one_customer_refund_orders,one_customer_refund_approval_audit FROM PUBLIC;
REVOKE ALL ON FUNCTION one_request_customer_refund_order(
 uuid,uuid,uuid,uuid,text,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION one_approve_customer_refund_order(
 uuid,uuid,uuid,text) FROM PUBLIC;
COMMIT;
