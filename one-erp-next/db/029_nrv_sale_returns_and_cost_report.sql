-- ONE ERP Next / 029 | Audit-ready NRV-adjusted sale report + safe returns gate.
-- PostgreSQL 14 CI ONLY. This is a deliberate fail-closed boundary:
-- returned stock from an NRV-affected sale must not silently regain
-- gross cost/COGS without a source-aware allowance recovery decision.
BEGIN;

CREATE FUNCTION one_nrv_adjusted_customer_return_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
BEGIN
 IF TG_OP='UPDATE' AND OLD.status='requested'
    AND NEW.status='completed' AND NEW.return_kind='customer'
    AND EXISTS(
       SELECT 1 FROM one_inventory_nrv_sale_releases r
       WHERE r.tenant_id=NEW.tenant_id AND
         r.stock_event_id=NEW.source_event_id
    ) THEN
   RAISE EXCEPTION
    'NRV-affected customer return requires source-linked gross cost and allowance recovery workflow';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER one_nrv_adjusted_customer_return_guard
 BEFORE UPDATE ON one_return_jobs
 FOR EACH ROW EXECUTE FUNCTION one_nrv_adjusted_customer_return_guard();

-- Auditors and authorized company users see the exact posted origin, not a
-- loose estimate or a duplicate source amount. This report is for NRV-
-- adjusted sales only, not all sales or an IFRS financial statement.
CREATE FUNCTION one_nrv_adjusted_sales_activity(
 p_tenant uuid,p_actor uuid,p_begins date,p_ends_exclusive date
) RETURNS TABLE(
 dispatched_events bigint,
 dispatched_quantity numeric,
 gross_cogs numeric,
 released_nrv_allowance numeric,
 net_cogs numeric,
 unresolved_events bigint,
 source_reconciled boolean
) LANGUAGE plpgsql STABLE SECURITY INVOKER AS $$
BEGIN
 IF p_tenant IS NULL OR p_actor IS NULL OR
    p_begins IS NULL OR p_ends_exclusive IS NULL OR
    p_begins>=p_ends_exclusive OR
    current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
    current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
    NOT EXISTS(SELECT 1 FROM one_memberships m
        WHERE m.tenant_id=p_tenant AND m.actor_id=p_actor
          AND m.active AND m.role IN ('company_admin','accountant','viewer')) THEN
   RAISE EXCEPTION 'Authorized company period report required';
 END IF;
 IF (SELECT count(DISTINCT j.currency)
    FROM one_inventory_nrv_sale_releases r
    JOIN one_journal_entries j ON j.tenant_id=r.tenant_id
      AND j.id=r.gross_cogs_journal_id
    WHERE r.tenant_id=p_tenant AND
      j.occurred_on>=p_begins AND j.occurred_on<p_ends_exclusive)>1 THEN
   RAISE EXCEPTION 'Mixed-currency NRV cost report needs functional-currency conversion';
 END IF;
 RETURN QUERY
 SELECT count(*)::bigint,
   coalesce(sum(r.quantity),0)::numeric,
   coalesce(sum(r.gross_cogs),0)::numeric,
   coalesce(sum(r.released_allowance),0)::numeric,
   coalesce(sum(r.net_cogs),0)::numeric,
   count(*) FILTER(WHERE NOT(
        src.movement_kind='sale_dispatch' AND src.qty=r.quantity
        AND src.amount=r.gross_cogs
        AND src.journal_id=r.gross_cogs_journal_id
        AND gross.status='posted' AND release.status='posted'
        AND gross.currency=release.currency
        AND release.occurred_on=gross.occurred_on
        AND gross.tenant_id=p_tenant AND release.tenant_id=p_tenant
   ))::bigint,
   coalesce(bool_and(
        src.movement_kind='sale_dispatch' AND src.qty=r.quantity
        AND src.amount=r.gross_cogs
        AND src.journal_id=r.gross_cogs_journal_id
        AND gross.status='posted' AND release.status='posted'
        AND gross.currency=release.currency
        AND release.occurred_on=gross.occurred_on
        AND gross.tenant_id=p_tenant AND release.tenant_id=p_tenant
   ),true)
 FROM one_inventory_nrv_sale_releases r
 JOIN one_stock_value_events src ON src.tenant_id=r.tenant_id
   AND src.stock_event_id=r.stock_event_id
 JOIN one_journal_entries gross ON gross.tenant_id=r.tenant_id
   AND gross.id=r.gross_cogs_journal_id
 JOIN one_journal_entries release ON release.tenant_id=r.tenant_id
   AND release.id=r.allowance_release_journal_id
 WHERE r.tenant_id=p_tenant
   AND gross.occurred_on>=p_begins
   AND gross.occurred_on<p_ends_exclusive;
END $$;
REVOKE ALL ON FUNCTION one_nrv_adjusted_customer_return_guard() FROM PUBLIC;
REVOKE ALL ON FUNCTION one_nrv_adjusted_sales_activity(
 uuid,uuid,date,date) FROM PUBLIC;
COMMIT;
