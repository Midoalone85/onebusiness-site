-- ONE ERP NEXT / 045 | Immutable observed-at-close inventory position seal.
-- Discrete, tested pilot. This is NOT a dated historical stock ledger:
-- stock movements currently record server insertion time, not trusted effective_on.
-- Therefore this captures the observed state when the close commits, NOT
-- balances reconstructed at ends_exclusive. Do not label it "period-end stock".
-- Do NOT deploy this migration to Oracle/production without operational review.
BEGIN;

CREATE TABLE one_period_inventory_seals (
 tenant_id uuid NOT NULL,
 period_id uuid NOT NULL,
 actor_id uuid NOT NULL,
 observed_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 snapshot_kind text NOT NULL DEFAULT 'OBSERVED_AT_CLOSE'
   CHECK(snapshot_kind='OBSERVED_AT_CLOSE'),
 position_count bigint NOT NULL CHECK(position_count>=0),
 physical_movement_count bigint NOT NULL CHECK(physical_movement_count>=0),
 valued_movement_count bigint NOT NULL CHECK(valued_movement_count>=0),
 nrv_adjustment_count bigint NOT NULL CHECK(nrv_adjustment_count>=0),
 PRIMARY KEY(tenant_id,period_id),
 FOREIGN KEY(tenant_id,period_id)
   REFERENCES one_period_close_audit(tenant_id,period_id)
);
ALTER TABLE one_period_inventory_seals ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_period_inventory_seals FORCE ROW LEVEL SECURITY;
CREATE POLICY one_period_inventory_seal_tenant ON one_period_inventory_seals
 USING(tenant_id::text=current_setting('one.tenant_id',true))
 WITH CHECK(tenant_id::text=current_setting('one.tenant_id',true));
CREATE TRIGGER one_period_inventory_seal_immutable
 BEFORE UPDATE OR DELETE ON one_period_inventory_seals
 FOR EACH ROW EXECUTE FUNCTION one_protect_audit();

CREATE TABLE one_period_inventory_seal_lines (
 tenant_id uuid NOT NULL,
 period_id uuid NOT NULL,
 warehouse_id uuid NOT NULL,
 product_id uuid NOT NULL,
 currency text NOT NULL REFERENCES one_currency_precision(code),
 physical_qty numeric(20,3) NOT NULL CHECK(physical_qty>=0),
 reserved_qty numeric(20,3) NOT NULL CHECK(reserved_qty>=0),
 valued_qty numeric(20,3) NOT NULL CHECK(valued_qty>=0),
 gross_book_value numeric(20,3) NOT NULL CHECK(gross_book_value>=0),
 nrv_allowance numeric(20,3) NOT NULL CHECK(nrv_allowance>=0),
 net_book_value numeric(20,3) NOT NULL CHECK(net_book_value>=0),
 PRIMARY KEY(tenant_id,period_id,warehouse_id,product_id),
 FOREIGN KEY(tenant_id,period_id)
   REFERENCES one_period_inventory_seals(tenant_id,period_id),
 CHECK(reserved_qty<=physical_qty),
 CHECK(physical_qty=valued_qty),
 CHECK(nrv_allowance<=gross_book_value),
 CHECK(net_book_value=gross_book_value-nrv_allowance)
);
ALTER TABLE one_period_inventory_seal_lines ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_period_inventory_seal_lines FORCE ROW LEVEL SECURITY;
CREATE POLICY one_period_inventory_seal_lines_tenant ON one_period_inventory_seal_lines
 USING(tenant_id::text=current_setting('one.tenant_id',true))
 WITH CHECK(tenant_id::text=current_setting('one.tenant_id',true));
CREATE TRIGGER one_period_inventory_seal_line_immutable
 BEFORE UPDATE OR DELETE ON one_period_inventory_seal_lines
 FOR EACH ROW EXECUTE FUNCTION one_protect_audit();

-- All rows are persisted in the same transaction as the 043 refund and 044
-- inventory evidence. Table locks are deliberately conservative in this CI
-- proof so a concurrent stock/valuation write cannot straddle capture.
-- They are global locks and NOT a scalable tenant-production locking design.
CREATE OR REPLACE FUNCTION one_close_period(
 p_tenant uuid,p_period uuid,p_actor uuid
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $close_with_observed_seal$
DECLARE
 p one_periods%ROWTYPE;
 refund record;
 inventory record;
 bad_nrv bigint;
 slots bigint;
 written bigint;
 movements bigint;
 valued bigint;
 nrv_count bigint;
BEGIN
 IF p_tenant IS NULL OR p_period IS NULL OR p_actor IS NULL OR
    current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
    current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
    NOT EXISTS(SELECT 1 FROM one_memberships m
      WHERE m.tenant_id=p_tenant AND m.actor_id=p_actor
        AND m.active AND m.role='company_admin') THEN
  RAISE EXCEPTION 'Authorized company admin required for close';
 END IF;

 -- Writers acquire ROW EXCLUSIVE on these tables. SHARE blocks those writes
 -- while the snapshot and the posted-ledger/refund gates are checked.
 LOCK TABLE one_stock_balances,one_stock_value_positions,
   one_inventory_nrv_positions,one_stock_events,
   one_stock_value_events,one_inventory_nrv_events IN SHARE MODE;

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

 -- Extra IAS 2 check; the 044 check only uses gross valued positions.
 -- Do not freeze a stock snapshot whose posted NRV contra ledger disagrees.
 SELECT count(*)::bigint INTO bad_nrv
 FROM one_inventory_nrv_positions n
 LEFT JOIN LATERAL one_inventory_nrv_reconciliation(
   p_tenant,n.warehouse_id,n.product_id,p_actor
 ) r ON true
 WHERE n.tenant_id=p_tenant AND
   (r.reconciliation_ok IS DISTINCT FROM TRUE OR
    r.net_carrying_amount IS NULL OR n.currency IS DISTINCT FROM (
      SELECT v.currency FROM one_stock_value_positions v
       WHERE v.tenant_id=n.tenant_id
         AND v.warehouse_id=n.warehouse_id AND v.product_id=n.product_id
    ));
 IF bad_nrv<>0 THEN
  RAISE EXCEPTION 'Close blocked: posted IAS 2 allowance reconciliation failed';
 END IF;

 SELECT count(*)::bigint INTO slots FROM one_stock_balances
   WHERE tenant_id=p_tenant;
 IF slots<>inventory.inspected_positions THEN
  RAISE EXCEPTION 'Inventory positions changed during close check';
 END IF;
 SELECT count(*)::bigint INTO movements FROM one_stock_events
  WHERE tenant_id=p_tenant;
 SELECT count(*)::bigint INTO valued FROM one_stock_value_events
  WHERE tenant_id=p_tenant;
 SELECT count(*)::bigint INTO nrv_count FROM one_inventory_nrv_events
  WHERE tenant_id=p_tenant;

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
 INSERT INTO one_period_inventory_seals(
   tenant_id,period_id,actor_id,position_count,
   physical_movement_count,valued_movement_count,nrv_adjustment_count)
 VALUES(p_tenant,p_period,p_actor,slots,movements,valued,nrv_count);

 INSERT INTO one_period_inventory_seal_lines(
  tenant_id,period_id,warehouse_id,product_id,currency,
  physical_qty,reserved_qty,valued_qty,gross_book_value,
  nrv_allowance,net_book_value)
 SELECT b.tenant_id,p_period,b.warehouse_id,b.product_id,v.currency,
   b.on_hand,b.reserved,v.qty,v.book_value,
   coalesce(n.allowance,0),v.book_value-coalesce(n.allowance,0)
 FROM one_stock_balances b
 JOIN one_stock_value_positions v
   ON v.tenant_id=b.tenant_id AND v.warehouse_id=b.warehouse_id
    AND v.product_id=b.product_id
 LEFT JOIN one_inventory_nrv_positions n
   ON n.tenant_id=b.tenant_id AND n.warehouse_id=b.warehouse_id
    AND n.product_id=b.product_id
 WHERE b.tenant_id=p_tenant;
 GET DIAGNOSTICS written=ROW_COUNT;
 IF written<>slots THEN
  RAISE EXCEPTION 'Inventory position capture count mismatch';
 END IF;
 RETURN p_period;
END $close_with_observed_seal$;

-- No mutable recomputation in reports: read the captured lines only.
-- Caller must be an active tenant member, and results are never aggregated
-- across currencies. A close-time observation is not a dated inventory GL.
CREATE FUNCTION one_inventory_period_seal_details(
 p_tenant uuid,p_period uuid,p_actor uuid
) RETURNS TABLE(
 warehouse_id uuid,product_id uuid,currency text,
 physical_qty numeric,reserved_qty numeric,valued_qty numeric,
 gross_book_value numeric,nrv_allowance numeric,net_book_value numeric,
 observed_at timestamptz,snapshot_kind text
) LANGUAGE plpgsql STABLE SECURITY INVOKER AS $read_seal$
BEGIN
 IF p_tenant IS NULL OR p_period IS NULL OR p_actor IS NULL OR
    current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
    current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
    NOT EXISTS(SELECT 1 FROM one_memberships m
      WHERE m.tenant_id=p_tenant AND m.actor_id=p_actor AND m.active
        AND m.role IN ('company_admin','accountant','viewer')) THEN
  RAISE EXCEPTION 'Authorized tenant reader required for inventory seal';
 END IF;
 IF NOT EXISTS (SELECT 1 FROM one_period_inventory_seals h
    WHERE h.tenant_id=p_tenant AND h.period_id=p_period) THEN
  RAISE EXCEPTION 'No captured close-time inventory seal for period';
 END IF;
 RETURN QUERY SELECT s.warehouse_id,s.product_id,s.currency,
   s.physical_qty::numeric,s.reserved_qty::numeric,s.valued_qty::numeric,
   s.gross_book_value::numeric,s.nrv_allowance::numeric,
   s.net_book_value::numeric,h.observed_at,h.snapshot_kind
 FROM one_period_inventory_seal_lines s
 JOIN one_period_inventory_seals h
   ON h.tenant_id=s.tenant_id AND h.period_id=s.period_id
 WHERE s.tenant_id=p_tenant AND s.period_id=p_period
 ORDER BY s.warehouse_id,s.product_id;
END $read_seal$;

REVOKE ALL ON one_period_inventory_seals,
  one_period_inventory_seal_lines FROM PUBLIC;
REVOKE ALL ON FUNCTION one_close_period(uuid,uuid,uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION one_inventory_period_seal_details(
  uuid,uuid,uuid) FROM PUBLIC;
COMMIT;
