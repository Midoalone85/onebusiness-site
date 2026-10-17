-- ONE ERP NEXT / 044 | Inventory-to-finance reconciliation at period close.
-- Experimental PostgreSQL tenant-scoped gate, NOT an audited year-end close.
-- Validates current stock positions globally (not a historical dated stock ledger).
BEGIN;

CREATE TABLE one_period_inventory_close_evidence (
 tenant_id uuid NOT NULL,
 period_id uuid NOT NULL,
 actor_id uuid NOT NULL,
 inspected_positions bigint NOT NULL CHECK(inspected_positions>=0),
 unreconciled_positions bigint NOT NULL CHECK(unreconciled_positions>=0),
 unvalued_movements bigint NOT NULL CHECK(unvalued_movements>=0),
 recorded_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 audit_policy text NOT NULL DEFAULT 'INVENTORY_CLOSE_GATE_V1'
   CHECK(audit_policy='INVENTORY_CLOSE_GATE_V1'),
 PRIMARY KEY(tenant_id,period_id),
 FOREIGN KEY(tenant_id,period_id)
   REFERENCES one_period_close_audit(tenant_id,period_id)
);
ALTER TABLE one_period_inventory_close_evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_period_inventory_close_evidence FORCE ROW LEVEL SECURITY;
CREATE POLICY one_period_inventory_evidence_scope
 ON one_period_inventory_close_evidence
 USING(tenant_id::text=current_setting('one.tenant_id',true))
 WITH CHECK(tenant_id::text=current_setting('one.tenant_id',true));
CREATE TRIGGER one_period_inventory_evidence_immutable
 BEFORE UPDATE OR DELETE ON one_period_inventory_close_evidence
 FOR EACH ROW EXECUTE FUNCTION one_protect_audit();

CREATE FUNCTION one_inventory_period_close_preflight(
 p_tenant uuid,p_period uuid,p_actor uuid
) RETURNS TABLE (
 inspected_positions bigint,
 unreconciled_positions bigint,
 unvalued_movements bigint,
 gate_passed boolean,
 note text
) LANGUAGE plpgsql STABLE SECURITY INVOKER AS $inventory_preflight$
DECLARE
 checked_count bigint;
 bad_count bigint;
 missing_cost bigint;
BEGIN
 IF p_tenant IS NULL OR p_period IS NULL OR p_actor IS NULL OR
    current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
    current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
    NOT EXISTS (
      SELECT 1 FROM one_memberships m
       WHERE m.tenant_id=p_tenant AND m.actor_id=p_actor
         AND m.active AND m.role='company_admin'
    ) THEN
  RAISE EXCEPTION 'Authorized company admin required for stock close';
 END IF;
 IF NOT EXISTS (
   SELECT 1 FROM one_periods p
    WHERE p.tenant_id=p_tenant AND p.id=p_period AND NOT p.closed
 ) THEN
  RAISE EXCEPTION 'Open company period required for stock close';
 END IF;

 -- FULL JOIN prevents a book-only stock position being silently omitted.
 -- Never sum monetary amounts across currencies in this pilot.
 WITH slots AS (
   SELECT coalesce(b.warehouse_id,v.warehouse_id) AS warehouse_id,
          coalesce(b.product_id,v.product_id) AS product_id,
          b.on_hand AS physical_qty,v.qty AS valued_qty
   FROM one_stock_balances b
   FULL OUTER JOIN one_stock_value_positions v
     ON v.tenant_id=b.tenant_id AND v.warehouse_id=b.warehouse_id
       AND v.product_id=b.product_id
   WHERE coalesce(b.tenant_id,v.tenant_id)=p_tenant
 ), checks AS (
   SELECT s.physical_qty,s.valued_qty,
          r.reconciliation_ok,r.unvalued_events
   FROM slots s
   LEFT JOIN LATERAL one_inventory_finance_reconciliation(
     p_tenant,s.warehouse_id,s.product_id,p_actor
   ) r ON true
 )
 SELECT count(*)::bigint,
   count(*) FILTER (
     WHERE physical_qty IS NULL OR valued_qty IS NULL OR
           reconciliation_ok IS DISTINCT FROM TRUE
   )::bigint,
   coalesce(sum(unvalued_events),0)::bigint
 INTO checked_count,bad_count,missing_cost FROM checks;
 RETURN QUERY SELECT checked_count,bad_count,missing_cost,
  (bad_count=0 AND missing_cost=0),
  'Current stock/valuation/GRNI snapshot only; not historical audit, physical count or IFRS signoff'::text;
END $inventory_preflight$;

-- Preserve 043 refund/GL gate, add inventory/valuation gate atomically.
CREATE OR REPLACE FUNCTION one_close_period(
 p_tenant uuid,p_period uuid,p_actor uuid
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $close_with_inventory_gate$
DECLARE
 p one_periods%ROWTYPE;
 refund record;
 inventory record;
BEGIN
 IF p_tenant IS NULL OR p_period IS NULL OR p_actor IS NULL OR
    current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
    current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
    NOT EXISTS (SELECT 1 FROM one_memberships m
       WHERE m.tenant_id=p_tenant AND m.actor_id=p_actor
         AND m.active AND m.role='company_admin') THEN
  RAISE EXCEPTION 'Authorized company admin required for close';
 END IF;
 SELECT * INTO p FROM one_periods
    WHERE tenant_id=p_tenant AND id=p_period FOR UPDATE;
 IF NOT FOUND OR p.closed THEN
  RAISE EXCEPTION 'Period missing or already closed';
 END IF;
 SELECT * INTO refund FROM one_refund_period_close_preflight(
     p_tenant,p_period,p_actor);
 IF NOT FOUND OR refund.gate_passed IS DISTINCT FROM TRUE THEN
  RAISE EXCEPTION 'Close blocked: draft journal or refund control mismatch';
 END IF;
 SELECT * INTO inventory FROM one_inventory_period_close_preflight(
     p_tenant,p_period,p_actor);
 IF NOT FOUND OR inventory.gate_passed IS DISTINCT FROM TRUE THEN
  RAISE EXCEPTION 'Close blocked: unvalued or unreconciled stock positions';
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
 VALUES(p_tenant,p_period,p_actor,refund.period_refund_claims,
   refund.period_refund_liability,refund.company_refund_liability,
   refund.company_unverified_bank_references);
 INSERT INTO one_period_inventory_close_evidence(
   tenant_id,period_id,actor_id,inspected_positions,
   unreconciled_positions,unvalued_movements)
 VALUES(p_tenant,p_period,p_actor,inventory.inspected_positions,
   inventory.unreconciled_positions,inventory.unvalued_movements);
 RETURN p_period;
END $close_with_inventory_gate$;

REVOKE ALL ON one_period_inventory_close_evidence FROM PUBLIC;
REVOKE ALL ON FUNCTION one_inventory_period_close_preflight(
 uuid,uuid,uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION one_close_period(uuid,uuid,uuid) FROM PUBLIC;
COMMIT;
