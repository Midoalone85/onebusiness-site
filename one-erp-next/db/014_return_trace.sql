-- ONE ERP Next / 014: Return case event trace + QA completion outbox.
-- Disposable PostgreSQL 14 CI only; never a production identity/API boundary.
BEGIN;

CREATE FUNCTION one_return_disposition_fact() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER AS $$
BEGIN
  INSERT INTO one_operation_outbox(tenant_id,event_type,source_id)
  VALUES (
    NEW.tenant_id,
    CASE NEW.action WHEN 'restock' THEN 'returns.restocked'
                    WHEN 'dispose' THEN 'returns.disposed'
                    ELSE NULL END,
    NEW.id
  );
  RETURN NEW;
END $$;

CREATE TRIGGER one_return_disposition_fact
 AFTER INSERT ON one_return_dispositions FOR EACH ROW
 EXECUTE FUNCTION one_return_disposition_fact();

-- Tenant-scoped operator timeline for one RMA. No names, addresses,
-- payments, personal health information or attachments are included.
CREATE FUNCTION one_return_case_timeline(
 p_tenant uuid, p_return uuid, p_actor uuid
) RETURNS TABLE(
 stream_position bigint, event_type text, source_id uuid,
 occurred_at timestamptz
) LANGUAGE plpgsql STABLE SECURITY INVOKER AS $$
BEGIN
 IF p_tenant IS NULL OR p_return IS NULL OR p_actor IS NULL OR
    current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
    current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
    NOT EXISTS(SELECT 1 FROM one_memberships m
       WHERE m.tenant_id=p_tenant AND m.actor_id=p_actor AND m.active
         AND m.role IN ('company_admin','accountant','viewer')) THEN
   RAISE EXCEPTION 'Return timeline requires active company membership';
 END IF;
 -- Avoid enumeration via a return ID belonging to another tenant.
 IF NOT EXISTS(SELECT 1 FROM one_return_jobs j
               WHERE j.tenant_id=p_tenant AND j.id=p_return) THEN
   RAISE EXCEPTION 'Return case not accessible';
 END IF;

 RETURN QUERY
 SELECT o.stream_position,o.event_type,o.source_id,o.created_at
 FROM one_operation_outbox o
 JOIN one_return_jobs j ON j.tenant_id=p_tenant AND j.id=p_return
 WHERE o.tenant_id=p_tenant
   AND (
      (o.source_id=j.id AND o.event_type LIKE 'returns.%')
      OR (o.source_id=j.source_event_id
          AND o.event_type IN ('stock.sale_dispatched','stock.purchase_received'))
      OR (o.source_id=j.processed_event_id
          AND o.event_type IN ('stock.customer_returned','stock.vendor_returned'))
      OR EXISTS (
         SELECT 1 FROM one_return_dispositions d
         WHERE d.tenant_id=j.tenant_id AND d.return_job_id=j.id
           AND (
             (o.source_id=d.id
                AND o.event_type IN ('returns.restocked','returns.disposed'))
             OR (o.source_id=d.stock_event_out_id AND
                 o.event_type IN ('stock.return_restock_out','stock.return_disposal'))
             OR (o.source_id=d.stock_event_in_id AND
                 o.event_type='stock.return_restock_in')
           )
      )
      OR EXISTS(
         SELECT 1 FROM one_customer_return_credits cr
         WHERE cr.tenant_id=j.tenant_id AND cr.return_job_id=j.id
           AND cr.credit_invoice_id=o.source_id
           AND o.event_type IN (
              'finance.customer_return_credited','finance.invoice_issued',
              'finance.journal_posted'
           )
      )
   )
 ORDER BY o.stream_position;
END $$;

REVOKE ALL ON FUNCTION one_return_case_timeline(uuid,uuid,uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION one_return_disposition_fact() FROM PUBLIC;
COMMIT;
