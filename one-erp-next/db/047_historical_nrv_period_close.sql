-- ONE ERP NEXT / 047: independently activated strict historical closing.
-- Prototype only. Uses PostgreSQL 14 disposable CI. This closes gaps between
-- the 045 observed snapshot and 046 source-by-source dated gross cost.
-- Only opt-in tenants receive mandatory historical cutoffs; legacy tenants
-- remain on the 045 observed-only path until a controlled migration.
-- No asserted IFRS/SOCPA certification, physical count, or production release.
BEGIN;

-- Activation is permanent and requires two independently invoked steps.
CREATE TABLE one_historical_close_activation_requests(
 tenant_id uuid PRIMARY KEY REFERENCES one_tenants(id),
 requested_by uuid NOT NULL,
 requested_at timestamptz NOT NULL DEFAULT transaction_timestamp()
);
CREATE TABLE one_historical_close_enrollments(
 tenant_id uuid PRIMARY KEY REFERENCES one_tenants(id),
 requested_by uuid NOT NULL,
 approved_by uuid NOT NULL,
 enrolled_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 audit_policy text NOT NULL DEFAULT 'DATED_CUTOFF_GROSS_AND_NRV_V1'
   CHECK(audit_policy='DATED_CUTOFF_GROSS_AND_NRV_V1'),
 CHECK(requested_by<>approved_by)
);
ALTER TABLE one_historical_close_activation_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_historical_close_activation_requests FORCE ROW LEVEL SECURITY;
CREATE POLICY one_historical_activation_request_scope
 ON one_historical_close_activation_requests
 USING(tenant_id::text=current_setting('one.tenant_id',true))
 WITH CHECK(tenant_id::text=current_setting('one.tenant_id',true));
ALTER TABLE one_historical_close_enrollments ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_historical_close_enrollments FORCE ROW LEVEL SECURITY;
CREATE POLICY one_historical_enrollment_scope
 ON one_historical_close_enrollments
 USING(tenant_id::text=current_setting('one.tenant_id',true))
 WITH CHECK(tenant_id::text=current_setting('one.tenant_id',true));

CREATE FUNCTION one_guard_historical_close_activation()
 RETURNS trigger LANGUAGE plpgsql AS $activation_guard$
BEGIN
 IF TG_OP<>'INSERT' OR
    current_setting('one.historical_activation_context',true)
      IS DISTINCT FROM 'yes' THEN
  RAISE EXCEPTION 'Historical close activation must use reviewed workflow';
 END IF;
 RETURN NEW;
END $activation_guard$;
CREATE TRIGGER one_historical_activation_request_guard
 BEFORE INSERT OR UPDATE OR DELETE ON one_historical_close_activation_requests
 FOR EACH ROW EXECUTE FUNCTION one_guard_historical_close_activation();
CREATE TRIGGER one_historical_enrollment_guard
 BEFORE INSERT OR UPDATE OR DELETE ON one_historical_close_enrollments
 FOR EACH ROW EXECUTE FUNCTION one_guard_historical_close_activation();

CREATE FUNCTION one_request_historical_inventory_close(
 p_tenant uuid,p_actor uuid
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $request_activation$
BEGIN
 IF p_tenant IS NULL OR p_actor IS NULL OR
   current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
   current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
   NOT EXISTS(SELECT 1 FROM one_memberships m
      WHERE m.tenant_id=p_tenant AND m.actor_id=p_actor
      AND m.active AND m.role='company_admin') THEN
  RAISE EXCEPTION 'Authorized administrator required for historical-close activation';
 END IF;
 PERFORM set_config('one.historical_activation_context','yes',true);
 INSERT INTO one_historical_close_activation_requests(tenant_id,requested_by)
 VALUES(p_tenant,p_actor);
 PERFORM set_config('one.historical_activation_context','',true);
 RETURN p_tenant;
END $request_activation$;

CREATE FUNCTION one_approve_historical_inventory_close(
 p_tenant uuid,p_actor uuid
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $approve_activation$
DECLARE req one_historical_close_activation_requests%ROWTYPE;
BEGIN
 IF p_tenant IS NULL OR p_actor IS NULL OR
   current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
   current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
   NOT EXISTS(SELECT 1 FROM one_memberships m
     WHERE m.tenant_id=p_tenant AND m.actor_id=p_actor
       AND m.active AND m.role IN ('company_admin','accountant')) THEN
  RAISE EXCEPTION 'Independent reviewer required for historical-close activation';
 END IF;
 SELECT * INTO req FROM one_historical_close_activation_requests
  WHERE tenant_id=p_tenant FOR UPDATE;
 IF NOT FOUND OR req.requested_by=p_actor OR
  NOT EXISTS(SELECT 1 FROM one_memberships m WHERE
    m.tenant_id=p_tenant AND m.actor_id=req.requested_by AND m.active
    AND m.role='company_admin') THEN
  RAISE EXCEPTION 'A separate active administrator request is required';
 END IF;
 PERFORM set_config('one.historical_activation_context','yes',true);
 INSERT INTO one_historical_close_enrollments(
   tenant_id,requested_by,approved_by)
 VALUES(p_tenant,req.requested_by,p_actor);
 PERFORM set_config('one.historical_activation_context','',true);
 RETURN p_tenant;
END $approve_activation$;

-- Dated net book = posted gross roll-forward minus posted IAS2 adjustments
-- plus derecognition of allowance on supported sales.
-- A missing/non-posted/mismatched NRV journal or sale release rejects the
-- entire result; no defaulting missing financial movements to zero.
CREATE FUNCTION one_inventory_historical_net_asof(
 p_tenant uuid,p_date date,p_actor uuid
) RETURNS TABLE(
 warehouse_id uuid,product_id uuid,currency text,
 physical_qty numeric,gross_book_value numeric,nrv_allowance numeric,
 net_book_value numeric,source_event_count bigint,
 as_of date,record_basis text
) LANGUAGE plpgsql STABLE SECURITY INVOKER AS $historic_net$
DECLARE bad bigint;
 rec record;
BEGIN
 IF p_tenant IS NULL OR p_actor IS NULL OR p_date IS NULL OR
    p_date>current_date OR
    current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
    current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
    NOT EXISTS(SELECT 1 FROM one_memberships m
      WHERE m.tenant_id=p_tenant AND m.actor_id=p_actor AND m.active
        AND m.role IN ('company_admin','accountant','viewer')) THEN
  RAISE EXCEPTION 'Authorized company member required for historical IAS2 read';
 END IF;
 SELECT count(*)::bigint INTO bad
 FROM one_inventory_nrv_events e
 LEFT JOIN one_journal_entries j
  ON j.tenant_id=e.tenant_id AND j.id=e.journal_id
 LEFT JOIN one_stock_value_positions v
  ON v.tenant_id=e.tenant_id AND v.warehouse_id=e.warehouse_id
     AND v.product_id=e.product_id
 WHERE e.tenant_id=p_tenant AND
  (j.status IS DISTINCT FROM 'posted' OR
   j.occurred_on IS DISTINCT FROM e.assessed_on OR
   v.currency IS NULL OR j.currency IS DISTINCT FROM v.currency);
 IF bad>0 THEN
  RAISE EXCEPTION 'Historical NRV assessment has inconsistent posted GL/date/currency';
 END IF;
 SELECT count(*)::bigint INTO bad
 FROM one_inventory_nrv_sale_releases r
 LEFT JOIN one_journal_entries j
  ON j.tenant_id=r.tenant_id AND j.id=r.allowance_release_journal_id
 LEFT JOIN one_journal_entries gross
  ON gross.tenant_id=r.tenant_id AND gross.id=r.gross_cogs_journal_id
 LEFT JOIN one_stock_value_events v
  ON v.tenant_id=r.tenant_id AND v.stock_event_id=r.stock_event_id
 LEFT JOIN one_inventory_effective_dates a
  ON a.tenant_id=r.tenant_id AND a.stock_event_id=r.stock_event_id
 WHERE r.tenant_id=p_tenant AND (
    j.status IS DISTINCT FROM 'posted' OR
    gross.status IS DISTINCT FROM 'posted' OR
    j.occurred_on IS DISTINCT FROM gross.occurred_on OR
    j.occurred_on IS DISTINCT FROM a.effective_on OR
    gross.id IS DISTINCT FROM v.journal_id OR
    j.currency IS DISTINCT FROM v.currency
 );
 IF bad>0 THEN
  RAISE EXCEPTION 'NRV sale release is not aligned with posted gross source and date';
 END IF;

 FOR rec IN
  WITH gross AS (
    SELECT * FROM one_inventory_gross_history_asof(p_tenant,p_date,p_actor)
  ), assessed AS (
    SELECT e.warehouse_id,e.product_id,
      sum(e.adjustment)::numeric AS allowance
    FROM one_inventory_nrv_events e
    JOIN one_journal_entries j
      ON j.tenant_id=e.tenant_id AND j.id=e.journal_id
    WHERE e.tenant_id=p_tenant AND j.occurred_on<=p_date
    GROUP BY e.warehouse_id,e.product_id
  ), released AS (
    SELECT r.warehouse_id,r.product_id,
      sum(r.released_allowance)::numeric AS release_amount
    FROM one_inventory_nrv_sale_releases r
    JOIN one_journal_entries j
      ON j.tenant_id=r.tenant_id AND j.id=r.allowance_release_journal_id
    WHERE r.tenant_id=p_tenant AND j.occurred_on<=p_date
    GROUP BY r.warehouse_id,r.product_id
  ), combined AS (
    SELECT g.warehouse_id,g.product_id,g.currency,
      g.physical_qty,g.gross_book_value,g.source_event_count,
      coalesce(a.allowance,0)-coalesce(r.release_amount,0) AS allowance
    FROM gross g
    LEFT JOIN assessed a
     ON a.warehouse_id=g.warehouse_id AND a.product_id=g.product_id
    LEFT JOIN released r
     ON r.warehouse_id=g.warehouse_id AND r.product_id=g.product_id
  )
  SELECT * FROM combined ORDER BY warehouse_id,product_id
 LOOP
  IF rec.allowance<0 OR rec.allowance>rec.gross_book_value OR
     (rec.physical_qty=0 AND rec.allowance<>0) THEN
   RAISE EXCEPTION 'Historical allowance exceeds period-end gross inventory';
  END IF;
  warehouse_id=rec.warehouse_id;product_id=rec.product_id;
  currency=rec.currency;physical_qty=rec.physical_qty;
  gross_book_value=rec.gross_book_value;nrv_allowance=rec.allowance;
  net_book_value=rec.gross_book_value-rec.allowance;
  source_event_count=rec.source_event_count;
  as_of=p_date;record_basis='POSTED_DATED_GROSS_PLUS_NRVALLOWANCE';
  RETURN NEXT;
 END LOOP;

 -- NRV movements may not exist without a matching dated gross inventory
 -- source bucket, even when there are no stock rows to return.
 IF EXISTS(
   SELECT 1 FROM one_inventory_nrv_events e
    JOIN one_journal_entries j ON j.tenant_id=e.tenant_id
       AND j.id=e.journal_id
    WHERE e.tenant_id=p_tenant AND j.occurred_on<=p_date
      AND NOT EXISTS(
       SELECT 1 FROM one_stock_events st
       JOIN one_inventory_effective_dates a ON
         a.tenant_id=st.tenant_id AND a.stock_event_id=st.id
       WHERE st.tenant_id=p_tenant AND st.warehouse_id=e.warehouse_id
        AND st.product_id=e.product_id AND a.effective_on<=p_date)
 ) THEN
  RAISE EXCEPTION 'Orphan historical NRV adjustment without source stock history';
 END IF;
END $historic_net$;

CREATE TABLE one_period_historical_inventory_close(
 tenant_id uuid NOT NULL,
 period_id uuid NOT NULL,
 actor_id uuid NOT NULL,
 cutoff_on date NOT NULL,
 position_count bigint NOT NULL CHECK(position_count>=0),
 recorded_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 evidence_kind text NOT NULL DEFAULT 'HISTORICAL_PERIOD_END_POSTED_GROSS_AND_NRV'
    CHECK(evidence_kind='HISTORICAL_PERIOD_END_POSTED_GROSS_AND_NRV'),
 PRIMARY KEY(tenant_id,period_id),
 FOREIGN KEY(tenant_id,period_id)
   REFERENCES one_period_close_audit(tenant_id,period_id)
);
CREATE TABLE one_period_historical_inventory_close_lines(
 tenant_id uuid NOT NULL,
 period_id uuid NOT NULL,
 warehouse_id uuid NOT NULL,
 product_id uuid NOT NULL,
 currency text NOT NULL REFERENCES one_currency_precision(code),
 physical_qty numeric(20,3) NOT NULL CHECK(physical_qty>=0),
 gross_book_value numeric(20,3) NOT NULL CHECK(gross_book_value>=0),
 nrv_allowance numeric(20,3) NOT NULL CHECK(nrv_allowance>=0),
 net_book_value numeric(20,3) NOT NULL CHECK(net_book_value>=0),
 source_event_count bigint NOT NULL CHECK(source_event_count>=0),
 PRIMARY KEY(tenant_id,period_id,warehouse_id,product_id),
 FOREIGN KEY(tenant_id,period_id)
  REFERENCES one_period_historical_inventory_close(tenant_id,period_id),
 CHECK(nrv_allowance<=gross_book_value),
 CHECK(net_book_value=gross_book_value-nrv_allowance)
);
ALTER TABLE one_period_historical_inventory_close ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_period_historical_inventory_close FORCE ROW LEVEL SECURITY;
CREATE POLICY one_historical_close_tenant
 ON one_period_historical_inventory_close
 USING(tenant_id::text=current_setting('one.tenant_id',true))
 WITH CHECK(tenant_id::text=current_setting('one.tenant_id',true));
ALTER TABLE one_period_historical_inventory_close_lines ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_period_historical_inventory_close_lines FORCE ROW LEVEL SECURITY;
CREATE POLICY one_historical_close_lines_tenant
 ON one_period_historical_inventory_close_lines
 USING(tenant_id::text=current_setting('one.tenant_id',true))
 WITH CHECK(tenant_id::text=current_setting('one.tenant_id',true));
CREATE TRIGGER one_historical_close_immutable
 BEFORE UPDATE OR DELETE ON one_period_historical_inventory_close
 FOR EACH ROW EXECUTE FUNCTION one_protect_audit();
CREATE TRIGGER one_historical_close_lines_immutable
 BEFORE UPDATE OR DELETE ON one_period_historical_inventory_close_lines
 FOR EACH ROW EXECUTE FUNCTION one_protect_audit();

-- Extend the 045 close operation rather than bypassing refund, IAS2-current,
-- source inventory or immutable observed-at-close audit checks.
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
 strict_mode boolean;
 cutoff date;
 historical_rows bigint;
 captured_rows bigint;
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
 -- These additional facts are locked for consistent historical gating.
 -- Conservative global locking is CI-only; replace with per-tenant locks.
 LOCK TABLE one_inventory_effective_dates,one_inventory_date_requests,
   one_inventory_nrv_sale_releases,one_return_dispositions,
   one_return_disposition_costs,one_journal_entries IN SHARE MODE;

 SELECT * INTO p FROM one_periods
   WHERE tenant_id=p_tenant AND id=p_period FOR UPDATE;
 IF NOT FOUND OR p.closed THEN
  RAISE EXCEPTION 'Period missing or already closed';
 END IF;
 SELECT EXISTS(SELECT 1 FROM one_historical_close_enrollments en
    WHERE en.tenant_id=p_tenant) INTO strict_mode;
 IF strict_mode THEN
  cutoff=p.ends_exclusive-1;
  IF cutoff>current_date THEN
   RAISE EXCEPTION 'Dated inventory close cannot certify a future period end';
  END IF;
  -- The 046 report is deliberately fail-closed on *any* missing stock date
  -- or cost source. 047 additionally verifies historical NRV adjustments.
  SELECT count(*)::bigint INTO historical_rows FROM
   one_inventory_historical_net_asof(p_tenant,cutoff,p_actor);
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
 IF strict_mode THEN
  INSERT INTO one_period_historical_inventory_close(
    tenant_id,period_id,actor_id,cutoff_on,position_count)
  VALUES(p_tenant,p_period,p_actor,cutoff,historical_rows);
  INSERT INTO one_period_historical_inventory_close_lines(
    tenant_id,period_id,warehouse_id,product_id,currency,physical_qty,
    gross_book_value,nrv_allowance,net_book_value,source_event_count)
  SELECT p_tenant,p_period,h.warehouse_id,h.product_id,h.currency,
    h.physical_qty,h.gross_book_value,h.nrv_allowance,
    h.net_book_value,h.source_event_count
  FROM one_inventory_historical_net_asof(p_tenant,cutoff,p_actor) h;
  GET DIAGNOSTICS captured_rows=ROW_COUNT;
  IF captured_rows<>historical_rows THEN
   RAISE EXCEPTION 'Historical cutoff line count changed during close';
  END IF;
  -- A period ending today must equal the immutable 045 observed snapshot.
  -- A prior period is a separately captured historical view, NOT today's stock.
  IF cutoff=current_date AND EXISTS(
     SELECT 1 FROM one_period_inventory_seal_lines s
     FULL JOIN one_period_historical_inventory_close_lines h
       ON h.tenant_id=s.tenant_id AND h.period_id=s.period_id
        AND h.warehouse_id=s.warehouse_id AND h.product_id=s.product_id
     WHERE coalesce(s.tenant_id,h.tenant_id)=p_tenant
       AND coalesce(s.period_id,h.period_id)=p_period
       AND (s.currency IS DISTINCT FROM h.currency OR
            coalesce(s.physical_qty,0)<>coalesce(h.physical_qty,0) OR
            coalesce(s.gross_book_value,0)<>coalesce(h.gross_book_value,0) OR
            coalesce(s.nrv_allowance,0)<>coalesce(h.nrv_allowance,0))
  ) THEN
    RAISE EXCEPTION 'Close-time stock and dated period-end stock do not match';
  END IF;
 END IF;
 RETURN p_period;
END $close_with_observed_seal$;

CREATE FUNCTION one_inventory_historical_close_details(
 p_tenant uuid,p_period uuid,p_actor uuid
) RETURNS TABLE(
 warehouse_id uuid,product_id uuid,currency text,
 physical_qty numeric,gross_book_value numeric,nrv_allowance numeric,
 net_book_value numeric,source_event_count bigint,
 cutoff_on date,evidence_kind text
) LANGUAGE plpgsql STABLE SECURITY INVOKER AS $sealed_history$
BEGIN
 IF p_tenant IS NULL OR p_period IS NULL OR p_actor IS NULL OR
    current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
    current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
    NOT EXISTS(SELECT 1 FROM one_memberships m WHERE m.tenant_id=p_tenant
      AND m.actor_id=p_actor AND m.active AND
      m.role IN ('company_admin','accountant','viewer')) THEN
  RAISE EXCEPTION 'Authorized company reader required for historical close seal';
 END IF;
 IF NOT EXISTS(SELECT 1 FROM one_period_historical_inventory_close h
    WHERE h.tenant_id=p_tenant AND h.period_id=p_period) THEN
  RAISE EXCEPTION 'No certified historical inventory cutoff for this period';
 END IF;
 RETURN QUERY
 SELECT l.warehouse_id,l.product_id,l.currency,
   l.physical_qty::numeric,l.gross_book_value::numeric,
   l.nrv_allowance::numeric,l.net_book_value::numeric,
   l.source_event_count,h.cutoff_on,h.evidence_kind
 FROM one_period_historical_inventory_close_lines l
 JOIN one_period_historical_inventory_close h ON
  h.tenant_id=l.tenant_id AND h.period_id=l.period_id
 WHERE l.tenant_id=p_tenant AND l.period_id=p_period
 ORDER BY l.warehouse_id,l.product_id;
END $sealed_history$;

REVOKE ALL ON one_historical_close_activation_requests,
 one_historical_close_enrollments,one_period_historical_inventory_close,
 one_period_historical_inventory_close_lines FROM PUBLIC;
REVOKE ALL ON FUNCTION one_request_historical_inventory_close(
 uuid,uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION one_approve_historical_inventory_close(
 uuid,uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION one_inventory_historical_net_asof(
 uuid,date,uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION one_inventory_historical_close_details(
 uuid,uuid,uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION one_close_period(uuid,uuid,uuid) FROM PUBLIC;
COMMIT;
