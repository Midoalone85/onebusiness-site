-- ONE ERP NEXT / 043 | Fail-closed finance-period gate for refund controls.
-- This is a bounded prototype: no IFRS statutory signoff, bank statement
-- verification, ZATCA issuance, or comprehensive enterprise close workflow.
BEGIN;

CREATE TABLE one_period_refund_close_evidence (
 tenant_id uuid NOT NULL,
 period_id uuid NOT NULL,
 actor_id uuid NOT NULL,
 posted_refund_claims bigint NOT NULL CHECK(posted_refund_claims>=0),
 period_refund_liability numeric(20,3) NOT NULL
    CHECK(period_refund_liability>=0),
 company_refund_liability numeric(20,3) NOT NULL
    CHECK(company_refund_liability>=0),
 company_unverified_bank_references numeric(20,3) NOT NULL
    CHECK(company_unverified_bank_references>=0),
 reviewed_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 audit_policy text NOT NULL DEFAULT 'REFUND_CLOSE_GATE_V1'
    CHECK(audit_policy='REFUND_CLOSE_GATE_V1'),
 PRIMARY KEY(tenant_id,period_id),
 FOREIGN KEY(tenant_id,period_id)
   REFERENCES one_period_close_audit(tenant_id,period_id)
);
ALTER TABLE one_period_refund_close_evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_period_refund_close_evidence FORCE ROW LEVEL SECURITY;
CREATE POLICY one_close_refund_evidence_scope
 ON one_period_refund_close_evidence
 USING(tenant_id::text=current_setting('one.tenant_id',true))
 WITH CHECK(tenant_id::text=current_setting('one.tenant_id',true));
CREATE TRIGGER one_close_refund_evidence_immutable BEFORE UPDATE OR DELETE
 ON one_period_refund_close_evidence FOR EACH ROW
 EXECUTE FUNCTION one_protect_audit();

CREATE FUNCTION one_refund_period_close_preflight(
 p_tenant uuid,p_period uuid,p_actor uuid
) RETURNS TABLE (
 period_open_journals bigint,
 period_refund_claims bigint,
 period_refund_liability numeric,
 period_refund_mismatches bigint,
 company_refund_liability numeric,
 company_unverified_bank_references numeric,
 company_refund_mismatches bigint,
 gate_passed boolean,
 note text
) LANGUAGE plpgsql STABLE SECURITY INVOKER AS $period_refund_preflight$
DECLARE
 p one_periods%ROWTYPE;
 register record;
 pulse record;
 waiting bigint;
BEGIN
 IF p_tenant IS NULL OR p_period IS NULL OR p_actor IS NULL OR
   current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
   current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
   NOT EXISTS(SELECT 1 FROM one_memberships m WHERE
     m.tenant_id=p_tenant AND m.actor_id=p_actor AND m.active
     AND m.role='company_admin') THEN
  RAISE EXCEPTION 'Company administrator required for close preflight';
 END IF;
 SELECT * INTO p FROM one_periods
    WHERE tenant_id=p_tenant AND id=p_period;
 IF NOT FOUND OR p.closed THEN
  RAISE EXCEPTION 'Open company accounting period required';
 END IF;
 SELECT count(*) INTO waiting FROM one_journal_entries j
    WHERE j.tenant_id=p_tenant AND j.period_id=p_period
      AND j.status<>'posted';
 SELECT * INTO register FROM one_nrv_customer_refund_liability_register(
    p_tenant,p_actor,p.begins,p.ends_exclusive);
 SELECT * INTO pulse FROM one_refund_control_pulse(p_tenant,p_actor);
 IF NOT FOUND OR register.recognized_claims IS NULL OR
   pulse.recognized_liability IS NULL THEN
  RAISE EXCEPTION 'Refund reconciliation evidence unavailable';
 END IF;
 RETURN QUERY SELECT waiting::bigint,register.recognized_claims::bigint,
    register.recognized_refund_liabilities::numeric,
    register.unverifiable_claims::bigint,
    pulse.recognized_liability::numeric,
    pulse.approved_with_unverified_reference::numeric,
    pulse.accounting_control_discrepancies::bigint,
    (waiting=0 AND register.unverifiable_claims=0
     AND register.accounting_reconciled IS TRUE
     AND pulse.accounting_control_discrepancies=0
     AND pulse.control_reconciled IS TRUE)::boolean,
    'Only refund+draft GL gate; external bank settlement unknown'::text;
END $period_refund_preflight$;

-- Extend the original 002 close operation: lock period, evaluate finance
-- gate, seal period and immutable audit together, or roll back everything.
-- A recognized refund payable may legitimately remain outstanding at close;
-- bank candidate references are evidence of NO settlement authority.
CREATE OR REPLACE FUNCTION one_close_period(
 p_tenant uuid,p_period uuid,p_actor uuid
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $close_with_refund_gate$
DECLARE
 p one_periods%ROWTYPE;
 result record;
BEGIN
 IF p_tenant IS NULL OR p_period IS NULL OR p_actor IS NULL OR
    current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
    current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
    NOT EXISTS(SELECT 1 FROM one_memberships WHERE tenant_id=p_tenant
      AND actor_id=p_actor AND active AND role='company_admin') THEN
  RAISE EXCEPTION 'Authorized company admin required for close';
 END IF;
 SELECT * INTO p FROM one_periods
    WHERE tenant_id=p_tenant AND id=p_period FOR UPDATE;
 IF NOT FOUND OR p.closed THEN
  RAISE EXCEPTION 'Period missing or already closed';
 END IF;
 SELECT * INTO result FROM one_refund_period_close_preflight(
     p_tenant,p_period,p_actor);
 IF NOT FOUND OR result.gate_passed IS DISTINCT FROM TRUE THEN
  RAISE EXCEPTION 'Cannot close: draft GL, refund source or control mismatch';
 END IF;
 PERFORM set_config('one.closing_context','yes',true);
 UPDATE one_periods SET closed=true,
   closed_at=transaction_timestamp(),closed_by=p_actor
 WHERE tenant_id=p_tenant AND id=p_period;
 PERFORM set_config('one.closing_context','',true);
 INSERT INTO one_period_close_audit(tenant_id,period_id,actor_id)
 VALUES(p_tenant,p_period,p_actor);
 INSERT INTO one_period_refund_close_evidence(
   tenant_id,period_id,actor_id,posted_refund_claims,
   period_refund_liability,company_refund_liability,
   company_unverified_bank_references)
 VALUES(p_tenant,p_period,p_actor,result.period_refund_claims,
   result.period_refund_liability,result.company_refund_liability,
   result.company_unverified_bank_references);
 RETURN p_period;
END $close_with_refund_gate$;

REVOKE ALL ON one_period_refund_close_evidence FROM PUBLIC;
REVOKE ALL ON FUNCTION one_refund_period_close_preflight(
 uuid,uuid,uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION one_close_period(uuid,uuid,uuid) FROM PUBLIC;
COMMIT;
