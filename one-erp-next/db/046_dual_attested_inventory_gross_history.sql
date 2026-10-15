-- ONE ERP NEXT / 046 | Dual-attested dated stock source registry + gross-cost roll-forward.
-- Tested independent PostgreSQL pilot; requires trusted backend identities and
-- hardened SQL grants in production. No data is silently backfilled from created_at.
-- Cost movements are dated by their posted journal; costed transfers require
-- an independently approved effective business date. Full IFRS/IAS2 net valuation,
-- open reservations over time, physical counts and production close integration
-- are NOT implemented in this migration.
BEGIN;

CREATE TABLE one_inventory_effective_dates (
 tenant_id uuid NOT NULL,
 stock_event_id uuid NOT NULL,
 effective_on date NOT NULL,
 source_kind text NOT NULL
   CHECK(source_kind IN ('posted_valuation','costed_disposition')),
 source_journal_id uuid,
 attested_by uuid NOT NULL,
 approved_by uuid NOT NULL,
 attested_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 audit_policy text NOT NULL DEFAULT 'DUAL_ATTESTED_EFFECTIVE_DATE_V1'
   CHECK(audit_policy='DUAL_ATTESTED_EFFECTIVE_DATE_V1'),
 PRIMARY KEY(tenant_id,stock_event_id),
 FOREIGN KEY(tenant_id,stock_event_id)
   REFERENCES one_stock_events(tenant_id,id),
 FOREIGN KEY(tenant_id,source_journal_id)
   REFERENCES one_journal_entries(tenant_id,id),
 CHECK(attested_by<>approved_by)
);
CREATE INDEX one_inventory_effective_dates_cutoff
 ON one_inventory_effective_dates(tenant_id,effective_on,stock_event_id);
ALTER TABLE one_inventory_effective_dates ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_inventory_effective_dates FORCE ROW LEVEL SECURITY;
CREATE POLICY one_inventory_effective_dates_tenant
 ON one_inventory_effective_dates
 USING(tenant_id::text=current_setting('one.tenant_id',true))
 WITH CHECK(tenant_id::text=current_setting('one.tenant_id',true));
CREATE FUNCTION one_inventory_date_row_guard()
 RETURNS trigger LANGUAGE plpgsql AS $row_guard$
BEGIN
 IF TG_OP<>'INSERT' OR
    current_setting('one.stock_date_registration',true) IS DISTINCT FROM 'yes' THEN
  RAISE EXCEPTION 'Inventory event business dates are append-only controlled facts';
 END IF;
 RETURN NEW;
END $row_guard$;
CREATE TRIGGER one_inventory_date_controlled
 BEFORE INSERT OR UPDATE OR DELETE ON one_inventory_effective_dates
 FOR EACH ROW EXECUTE FUNCTION one_inventory_date_row_guard();


-- Request and approval are separate authenticated invoker calls. Recording a
-- reviewer UUID in the first request would NOT constitute real approval.
CREATE TABLE one_inventory_date_requests (
 tenant_id uuid NOT NULL,
 stock_event_id uuid NOT NULL,
 proposed_on date NOT NULL,
 requested_by uuid NOT NULL,
 requested_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 PRIMARY KEY(tenant_id,stock_event_id),
 FOREIGN KEY(tenant_id,stock_event_id)
   REFERENCES one_stock_events(tenant_id,id)
);
ALTER TABLE one_inventory_date_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_inventory_date_requests FORCE ROW LEVEL SECURITY;
CREATE POLICY one_inventory_date_request_tenant ON one_inventory_date_requests
 USING(tenant_id::text=current_setting('one.tenant_id',true))
 WITH CHECK(tenant_id::text=current_setting('one.tenant_id',true));
CREATE FUNCTION one_inventory_date_request_guard()
 RETURNS trigger LANGUAGE plpgsql AS $pending_guard$
BEGIN
 IF TG_OP<>'INSERT' OR
    current_setting('one.stock_date_request_context',true) IS DISTINCT FROM 'yes' THEN
  RAISE EXCEPTION 'Stock date requests can only be opened via controlled workflow';
 END IF;
 RETURN NEW;
END $pending_guard$;
CREATE TRIGGER one_inventory_date_request_controlled
 BEFORE INSERT OR UPDATE OR DELETE ON one_inventory_date_requests
 FOR EACH ROW EXECUTE FUNCTION one_inventory_date_request_guard();

CREATE FUNCTION one_request_inventory_effective_date(
 p_tenant uuid,p_event uuid,p_date date,p_actor uuid
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $request_stock_date$
BEGIN
 IF p_tenant IS NULL OR p_event IS NULL OR p_date IS NULL OR
    p_actor IS NULL OR p_date>current_date OR
    current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
    current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
    NOT EXISTS(SELECT 1 FROM one_memberships m
      WHERE m.tenant_id=p_tenant AND m.actor_id=p_actor
        AND m.active AND m.role='company_admin') THEN
  RAISE EXCEPTION 'Authorized company administrator required to request effective date';
 END IF;
 PERFORM 1 FROM one_periods p
   WHERE p.tenant_id=p_tenant AND p.begins<=p_date
     AND p.ends_exclusive>p_date AND NOT p.closed FOR UPDATE;
 IF NOT FOUND THEN
  RAISE EXCEPTION 'Open accounting period required for stock date request';
 END IF;
 PERFORM 1 FROM one_stock_events e
   WHERE e.tenant_id=p_tenant AND e.id=p_event;
 IF NOT FOUND THEN RAISE EXCEPTION 'Cannot date a missing stock movement'; END IF;
 PERFORM set_config('one.stock_date_request_context','yes',true);
 INSERT INTO one_inventory_date_requests(
   tenant_id,stock_event_id,proposed_on,requested_by)
 VALUES(p_tenant,p_event,p_date,p_actor);
 PERFORM set_config('one.stock_date_request_context','',true);
 RETURN p_event;
END $request_stock_date$;

-- Authenticated identity/permissions are inputs from a trusted backend.
-- GUCs and these checks alone are NOT sufficient user authentication.
CREATE FUNCTION one_approve_inventory_effective_date(
 p_tenant uuid,p_event uuid,p_actor uuid
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $attest_stock_date$
DECLARE
 ev one_stock_events%ROWTYPE;
 request one_inventory_date_requests%ROWTYPE;
 valuation one_stock_value_events%ROWTYPE;
 disposition one_return_dispositions%ROWTYPE;
 cost one_return_disposition_costs%ROWTYPE;
 posted one_journal_entries%ROWTYPE;
 v_kind text;
 source_journal uuid;
 p_date date;
BEGIN
 IF p_tenant IS NULL OR p_event IS NULL OR p_actor IS NULL OR
    current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
    current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
    NOT EXISTS(SELECT 1 FROM one_memberships m
      WHERE m.tenant_id=p_tenant AND m.actor_id=p_actor
        AND m.active AND m.role IN ('company_admin','accountant')) THEN
  RAISE EXCEPTION 'Authenticated independent reviewer required';
 END IF;
 SELECT * INTO request FROM one_inventory_date_requests
   WHERE tenant_id=p_tenant AND stock_event_id=p_event FOR UPDATE;
 IF NOT FOUND OR request.requested_by=p_actor THEN
  RAISE EXCEPTION 'An independent first-party request is required';
 END IF;
 IF NOT EXISTS(SELECT 1 FROM one_memberships m
    WHERE m.tenant_id=p_tenant AND m.actor_id=request.requested_by
      AND m.active AND m.role='company_admin') THEN
  RAISE EXCEPTION 'Original stock-date requester is no longer authorized';
 END IF;
 p_date=request.proposed_on;
 IF p_date>current_date THEN
  RAISE EXCEPTION 'Future-effective stock history is not yet supported';
 END IF;
 -- An accounting close locks the same period row. Atomic approval cannot
 -- interleave with close, and closed periods reject late registration.
 PERFORM 1 FROM one_periods p
  WHERE p.tenant_id=p_tenant AND p.begins<=p_date
    AND p.ends_exclusive>p_date AND NOT p.closed FOR UPDATE;
 IF NOT FOUND THEN
  RAISE EXCEPTION 'Open accounting period for dated stock activity required';
 END IF;
 SELECT * INTO ev FROM one_stock_events
   WHERE tenant_id=p_tenant AND id=p_event FOR SHARE;
 IF NOT FOUND THEN RAISE EXCEPTION 'Stock source event unavailable'; END IF;
 IF EXISTS(SELECT 1 FROM one_inventory_effective_dates
   WHERE tenant_id=p_tenant AND stock_event_id=p_event) THEN
  RAISE EXCEPTION 'Source stock business date already attested';
 END IF;

 SELECT * INTO valuation FROM one_stock_value_events
   WHERE tenant_id=p_tenant AND stock_event_id=p_event;
 IF FOUND THEN
  IF valuation.movement_kind<>ev.movement_kind OR
     valuation.qty<>ev.quantity THEN
   RAISE EXCEPTION 'Source quantity/kind and valued movement do not match';
  END IF;
  SELECT * INTO posted FROM one_journal_entries
   WHERE tenant_id=p_tenant AND id=valuation.journal_id;
  IF NOT FOUND OR posted.status<>'posted' OR
     posted.occurred_on<>p_date OR posted.currency<>valuation.currency THEN
   RAISE EXCEPTION 'Effective business date must equal the posted stock journal date';
  END IF;
  v_kind='posted_valuation';
  source_journal=valuation.journal_id;
 ELSE
  SELECT * INTO disposition FROM one_return_dispositions d
   WHERE d.tenant_id=p_tenant AND
     (d.stock_event_out_id=p_event OR d.stock_event_in_id=p_event);
  IF NOT FOUND THEN
   RAISE EXCEPTION 'Unvalued or uncosted stock activity is not dateable';
  END IF;
  SELECT * INTO cost FROM one_return_disposition_costs c
   WHERE c.tenant_id=p_tenant AND c.disposition_id=disposition.id;
  IF NOT FOUND OR cost.action<>disposition.action OR
     cost.quantity<>disposition.quantity OR ev.quantity<>cost.quantity THEN
   RAISE EXCEPTION 'Completed and costed return disposition required';
  END IF;
  IF (disposition.action='restock' AND (
         (p_event=disposition.stock_event_out_id AND
            ev.movement_kind<>'return_restock_out') OR
         (p_event=disposition.stock_event_in_id AND
            ev.movement_kind<>'return_restock_in'))) OR
     (disposition.action='dispose' AND
         (p_event<>disposition.stock_event_out_id OR
            ev.movement_kind<>'return_disposal')) THEN
   RAISE EXCEPTION 'Disposition source event identity mismatch';
  END IF;
  IF disposition.action='dispose' THEN
   SELECT * INTO posted FROM one_journal_entries
     WHERE tenant_id=p_tenant AND id=cost.journal_id;
   IF NOT FOUND OR posted.status<>'posted' OR posted.occurred_on<>p_date THEN
    RAISE EXCEPTION 'Disposal effective date must match its posted journal';
   END IF;
   source_journal=cost.journal_id;
  ELSE
   source_journal=NULL;
  END IF;
  -- Out/in transfer are one operation, never spread over two business dates.
  IF EXISTS(
   SELECT 1 FROM one_inventory_effective_dates a
   WHERE a.tenant_id=p_tenant AND
     a.stock_event_id IN (
       disposition.stock_event_out_id,disposition.stock_event_in_id
     ) AND a.effective_on<>p_date
  ) THEN
   RAISE EXCEPTION 'Both sides of a stock transfer require the same effective date';
  END IF;
  v_kind='costed_disposition';
 END IF;

 -- Weighted-average costing uses actual posting sequence. Reordering
 -- economic dates across the same warehouse/product would invalidate COGS.
 IF EXISTS(
  SELECT 1 FROM one_inventory_effective_dates a
  JOIN one_stock_events x
    ON x.tenant_id=a.tenant_id AND x.id=a.stock_event_id
  WHERE a.tenant_id=p_tenant
    AND x.warehouse_id=ev.warehouse_id AND x.product_id=ev.product_id
    AND ((x.created_at<ev.created_at AND a.effective_on>p_date) OR
         (x.created_at>ev.created_at AND a.effective_on<p_date))
 ) THEN
  RAISE EXCEPTION 'Stock dates cannot reverse the immutable costing order';
 END IF;

 PERFORM set_config('one.stock_date_registration','yes',true);
 INSERT INTO one_inventory_effective_dates(
  tenant_id,stock_event_id,effective_on,source_kind,source_journal_id,
  attested_by,approved_by)
 VALUES(p_tenant,p_event,p_date,v_kind,source_journal,request.requested_by,p_actor);
 PERFORM set_config('one.stock_date_registration','',true);
 RETURN p_event;
END $attest_stock_date$;

-- As-of report refuses ANY undated, unvalued or partially costed source in the
-- company. This intentionally blocks historical reports after a new activity
-- until its dates are verified. No untrusted timestamp backfill / silent 0 cost.
CREATE FUNCTION one_inventory_gross_history_asof(
 p_tenant uuid,p_date date,p_actor uuid
) RETURNS TABLE(
 warehouse_id uuid, product_id uuid, currency text,
 physical_qty numeric,gross_book_value numeric,
 source_event_count bigint,as_of date,record_basis text
) LANGUAGE plpgsql STABLE SECURITY INVOKER AS $gross_asof$
DECLARE
 n_bad bigint;
 pos record;
 live one_stock_balances%ROWTYPE;
 valued one_stock_value_positions%ROWTYPE;
BEGIN
 IF p_tenant IS NULL OR p_date IS NULL OR p_actor IS NULL OR
    p_date>current_date OR
    current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
    current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
    NOT EXISTS(SELECT 1 FROM one_memberships m WHERE m.tenant_id=p_tenant
      AND m.actor_id=p_actor AND m.active
      AND m.role IN ('company_admin','accountant','viewer')) THEN
  RAISE EXCEPTION 'Authorized tenant member required for dated stock history';
 END IF;
 SELECT count(*)::bigint INTO n_bad
 FROM one_stock_events ev
 LEFT JOIN one_inventory_effective_dates a
  ON a.tenant_id=ev.tenant_id AND a.stock_event_id=ev.id
 LEFT JOIN one_stock_value_events v
  ON v.tenant_id=ev.tenant_id AND v.stock_event_id=ev.id
 LEFT JOIN one_journal_entries j
  ON j.tenant_id=v.tenant_id AND j.id=v.journal_id
 LEFT JOIN one_return_dispositions d
  ON d.tenant_id=ev.tenant_id AND
   (d.stock_event_out_id=ev.id OR d.stock_event_in_id=ev.id)
 LEFT JOIN one_return_disposition_costs dc
  ON dc.tenant_id=d.tenant_id AND dc.disposition_id=d.id
 LEFT JOIN one_journal_entries dj
  ON dj.tenant_id=dc.tenant_id AND dj.id=dc.journal_id
 WHERE ev.tenant_id=p_tenant AND (
   a.stock_event_id IS NULL OR
   ((v.stock_event_id IS NULL)=(dc.disposition_id IS NULL)) OR
   (v.stock_event_id IS NOT NULL AND
      (a.source_kind<>'posted_valuation' OR
       a.source_journal_id IS DISTINCT FROM v.journal_id OR
       v.qty<>ev.quantity OR v.movement_kind<>ev.movement_kind OR
       j.status IS DISTINCT FROM 'posted' OR
       j.occurred_on IS DISTINCT FROM a.effective_on OR
       j.currency IS DISTINCT FROM v.currency)) OR
   (dc.disposition_id IS NOT NULL AND
      (a.source_kind<>'costed_disposition' OR
       dc.quantity<>ev.quantity OR
       (d.action='dispose' AND
        (dj.status IS DISTINCT FROM 'posted' OR
         dj.occurred_on IS DISTINCT FROM a.effective_on OR
         a.source_journal_id IS DISTINCT FROM dc.journal_id)) OR
       (d.action='restock' AND a.source_journal_id IS NOT NULL) OR
       (d.action='restock' AND (
          (SELECT other.effective_on FROM one_inventory_effective_dates other
           WHERE other.tenant_id=ev.tenant_id AND
            other.stock_event_id=CASE
             WHEN ev.id=d.stock_event_out_id THEN d.stock_event_in_id
             ELSE d.stock_event_out_id END) IS DISTINCT FROM a.effective_on))
       ))
 );
 IF n_bad<>0 THEN
  RAISE EXCEPTION 'Incomplete dated/posted/valued inventory event coverage: %',n_bad;
 END IF;

 FOR pos IN
 WITH events AS (
  SELECT ev.warehouse_id AS wh,ev.product_id AS product,
   coalesce(v.currency,p.currency) AS cur,
   CASE WHEN ev.movement_kind IN
     ('purchase_receipt','customer_return','return_restock_in')
      THEN ev.quantity ELSE -ev.quantity END AS signed_qty,
   CASE WHEN ev.movement_kind IN
     ('purchase_receipt','customer_return','return_restock_in')
      THEN coalesce(v.amount,dc.amount)
      ELSE -coalesce(v.amount,dc.amount) END AS signed_cost
  FROM one_stock_events ev
  JOIN one_inventory_effective_dates a
   ON a.tenant_id=ev.tenant_id AND a.stock_event_id=ev.id
  LEFT JOIN one_stock_value_events v
   ON v.tenant_id=ev.tenant_id AND v.stock_event_id=ev.id
  LEFT JOIN one_return_dispositions d
   ON d.tenant_id=ev.tenant_id AND
      (d.stock_event_out_id=ev.id OR d.stock_event_in_id=ev.id)
  LEFT JOIN one_return_disposition_costs dc
   ON dc.tenant_id=d.tenant_id AND dc.disposition_id=d.id
  LEFT JOIN one_stock_value_positions p
   ON p.tenant_id=ev.tenant_id
     AND p.warehouse_id=ev.warehouse_id AND p.product_id=ev.product_id
  WHERE ev.tenant_id=p_tenant AND a.effective_on<=p_date
 ), balances AS (
  SELECT wh,product,cur,
    sum(signed_qty)::numeric AS qty,
    sum(signed_cost)::numeric AS gross,
    count(*)::bigint AS event_count
  FROM events GROUP BY wh,product,cur
 )
 SELECT wh,product,cur,qty,gross,event_count
 FROM balances ORDER BY wh,product,cur
 LOOP
  IF pos.cur IS NULL OR pos.qty<0 OR pos.gross<0 THEN
   RAISE EXCEPTION 'Dated warehouse stock contains invalid currency/negative cost or quantity';
  END IF;
  warehouse_id=pos.wh;product_id=pos.product;currency=pos.cur;
  physical_qty=pos.qty;gross_book_value=pos.gross;
  source_event_count=pos.event_count;as_of=p_date;
  record_basis='DUAL_ATTESTED_BUSINESS_DATE_GROSS_ONLY';
  RETURN NEXT;
 END LOOP;

 -- Current date must reconcile to the live inventory if every event has
 -- been attested. This detects any missing position/activity source.
 IF p_date=current_date AND EXISTS(
   WITH events AS (
    SELECT ev.warehouse_id AS wh,ev.product_id AS product,
      sum(CASE WHEN ev.movement_kind IN
       ('purchase_receipt','customer_return','return_restock_in')
       THEN ev.quantity ELSE -ev.quantity END) AS qty,
      sum(CASE WHEN ev.movement_kind IN
       ('purchase_receipt','customer_return','return_restock_in')
       THEN coalesce(v.amount,dc.amount)
       ELSE -coalesce(v.amount,dc.amount) END) AS gross
    FROM one_stock_events ev
    LEFT JOIN one_stock_value_events v
      ON v.tenant_id=ev.tenant_id AND v.stock_event_id=ev.id
    LEFT JOIN one_return_dispositions d
      ON d.tenant_id=ev.tenant_id AND
         (d.stock_event_out_id=ev.id OR d.stock_event_in_id=ev.id)
    LEFT JOIN one_return_disposition_costs dc
      ON dc.tenant_id=d.tenant_id AND dc.disposition_id=d.id
    WHERE ev.tenant_id=p_tenant
    GROUP BY ev.warehouse_id,ev.product_id
   ), slots AS (
    SELECT coalesce(e.wh,b.warehouse_id,v.warehouse_id) wh,
      coalesce(e.product,b.product_id,v.product_id) product,
      e.qty,e.gross,b.on_hand,v.qty AS book_qty,v.book_value
    FROM events e
    FULL JOIN one_stock_balances b
     ON b.tenant_id=p_tenant AND b.warehouse_id=e.wh
       AND b.product_id=e.product
    FULL JOIN one_stock_value_positions v
     ON v.tenant_id=p_tenant
       AND v.warehouse_id=coalesce(e.wh,b.warehouse_id)
       AND v.product_id=coalesce(e.product,b.product_id)
   )
   SELECT 1 FROM slots WHERE
    coalesce(qty,0)<>coalesce(on_hand,0) OR
    coalesce(qty,0)<>coalesce(book_qty,0) OR
    coalesce(gross,0)<>coalesce(book_value,0)
 ) THEN
  RAISE EXCEPTION 'Dated gross stock roll-forward does not equal current physical/valued inventory';
 END IF;
END $gross_asof$;

REVOKE ALL ON one_inventory_effective_dates,
 one_inventory_date_requests FROM PUBLIC;
REVOKE ALL ON FUNCTION one_request_inventory_effective_date(
 uuid,uuid,date,uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION one_approve_inventory_effective_date(
 uuid,uuid,uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION one_inventory_gross_history_asof(
 uuid,date,uuid) FROM PUBLIC;
COMMIT;
