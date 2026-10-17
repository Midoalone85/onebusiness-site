-- ONE ERP Next / 026 | Read-only IAS 2 inventory reconciliation and disclosure.
-- Financial-control schedule for isolated PostgreSQL 14 development only.
-- Cost vs NRV uses stored tested adjustments; no official financial statements.
BEGIN;

CREATE FUNCTION one_inventory_nrv_company_snapshot(
 p_tenant uuid,p_actor uuid
) RETURNS TABLE(
 stock_buckets bigint,
 physical_units numeric,
 valued_units numeric,
 gross_cost numeric,
 allowance_balance numeric,
 net_carrying_value numeric,
 allowance_gl_balance numeric,
 unreconciled_buckets bigint,
 fully_reconciled boolean
) LANGUAGE plpgsql STABLE SECURITY INVOKER AS $$
BEGIN
 IF p_tenant IS NULL OR p_actor IS NULL OR
    current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
    current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
    NOT EXISTS(SELECT 1 FROM one_memberships m
      WHERE m.tenant_id=p_tenant AND m.actor_id=p_actor AND m.active
        AND m.role IN ('company_admin','accountant','viewer')) THEN
   RAISE EXCEPTION 'Approved company financial reporting role required';
 END IF;
 -- No functional-currency FX translation engine exists yet. Never sum
 -- inventory figures expressed in different transaction currencies.
 IF (SELECT count(DISTINCT v.currency) FROM one_stock_value_positions v
      WHERE v.tenant_id=p_tenant)>1 THEN
   RAISE EXCEPTION 'Company inventory contains multiple valuation currencies: translate before aggregation';
 END IF;
 RETURN QUERY
 SELECT count(*)::bigint,
   coalesce(sum(b.on_hand),0)::numeric,
   coalesce(sum(r.valued_qty),0)::numeric,
   coalesce(sum(r.gross_cost),0)::numeric,
   coalesce(sum(r.allowance),0)::numeric,
   coalesce(sum(r.net_carrying_amount),0)::numeric,
   coalesce(sum(r.posted_allowance),0)::numeric,
   count(*) FILTER (WHERE r.reconciliation_ok IS DISTINCT FROM true)::bigint,
   coalesce(bool_and(r.reconciliation_ok),true)
 FROM one_stock_balances b
 LEFT JOIN LATERAL one_inventory_nrv_reconciliation(
   b.tenant_id,b.warehouse_id,b.product_id,p_actor
 ) r ON TRUE
 WHERE b.tenant_id=p_tenant;
END $$;

CREATE FUNCTION one_inventory_nrv_period_activity(
 p_tenant uuid,p_actor uuid,p_start date,p_end_exclusive date
) RETURNS TABLE(
 adjustments bigint,
 write_down_recognized numeric,
 reversals_recognized numeric,
 net_loss_recognized numeric,
 last_assessment date
) LANGUAGE plpgsql STABLE SECURITY INVOKER AS $$
BEGIN
 IF p_tenant IS NULL OR p_actor IS NULL OR p_start IS NULL OR
    p_end_exclusive IS NULL OR p_end_exclusive<=p_start OR
    current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
    current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
    NOT EXISTS(SELECT 1 FROM one_memberships m
      WHERE m.tenant_id=p_tenant AND m.actor_id=p_actor AND m.active
        AND m.role IN ('company_admin','accountant','viewer')) THEN
   RAISE EXCEPTION 'Approved period / tenant reader required';
 END IF;
 IF (SELECT count(DISTINCT j.currency)
      FROM one_inventory_nrv_events e
      JOIN one_journal_entries j ON j.tenant_id=e.tenant_id
       AND j.id=e.journal_id
      WHERE e.tenant_id=p_tenant AND e.assessed_on>=p_start
        AND e.assessed_on<p_end_exclusive AND j.status='posted')>1 THEN
   RAISE EXCEPTION 'Multi-currency NRV activity requires reviewed functional currency conversion';
 END IF;
 RETURN QUERY
 SELECT count(*)::bigint,
   coalesce(sum(greatest(e.adjustment,0)),0)::numeric,
   coalesce(sum(greatest(-e.adjustment,0)),0)::numeric,
   coalesce(sum(e.adjustment),0)::numeric,
   max(e.assessed_on)
 FROM one_inventory_nrv_events e
 JOIN one_journal_entries j
   ON j.tenant_id=e.tenant_id AND j.id=e.journal_id
 WHERE e.tenant_id=p_tenant
   AND e.assessed_on>=p_start AND e.assessed_on<p_end_exclusive
   AND j.status='posted'
   AND j.occurred_on=e.assessed_on;
END $$;

REVOKE ALL ON FUNCTION one_inventory_nrv_company_snapshot(uuid,uuid)
 FROM PUBLIC;
REVOKE ALL ON FUNCTION one_inventory_nrv_period_activity(
 uuid,uuid,date,date) FROM PUBLIC;
COMMIT;
