-- ONE ERP Next: durable business event outbox for independent module consumers.
-- CI/PROTOTYPE ONLY: atomic event facts, not a message broker or live integration.
-- Only identifiers, no customer names, patient info, token or PII in payload.
BEGIN;

CREATE TABLE one_operation_outbox (
  tenant_id uuid NOT NULL REFERENCES one_tenants(id),
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  stream_position bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
  event_type text NOT NULL CHECK (event_type IN (
    'stock.purchase_received','stock.sale_dispatched',
    'delivery.created','delivery.picked','delivery.packed',
    'delivery.dispatched','delivery.delivered',
    'receiving.inspection_started','receiving.inspection_completed',
    'finance.invoice_issued','finance.journal_posted'
  )),
  source_id uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
  PRIMARY KEY (tenant_id,id),
  UNIQUE (tenant_id,event_type,source_id)
);
CREATE INDEX one_operation_outbox_scan
  ON one_operation_outbox(tenant_id,stream_position);
ALTER TABLE one_operation_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_operation_outbox FORCE ROW LEVEL SECURITY;
CREATE POLICY one_operation_outbox_tenant ON one_operation_outbox
  USING (tenant_id::text=current_setting('one.tenant_id',true))
  WITH CHECK (tenant_id::text=current_setting('one.tenant_id',true));
CREATE TRIGGER one_operation_outbox_immutable
  BEFORE UPDATE OR DELETE ON one_operation_outbox
  FOR EACH ROW EXECUTE FUNCTION one_protect_audit();

CREATE FUNCTION one_route_operation_event() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE v_kind text; v_source uuid;
BEGIN
  IF TG_TABLE_NAME='one_stock_events' THEN
    v_kind=CASE NEW.movement_kind
      WHEN 'purchase_receipt' THEN 'stock.purchase_received'
      WHEN 'sale_dispatch' THEN 'stock.sale_dispatched'
      ELSE NULL END;
    v_source=NEW.id;
  ELSIF TG_TABLE_NAME='one_fulfillment_audit' THEN
    v_kind='delivery.'||NEW.event;
    v_source=NEW.delivery_id;
  ELSIF TG_TABLE_NAME='one_receiving_audit' THEN
    v_kind='receiving.'||NEW.event;
    v_source=NEW.inspection_id;
  ELSIF TG_TABLE_NAME='one_invoice_audit' THEN
    v_kind='finance.invoice_issued';
    v_source=NEW.doc_id;
  ELSIF TG_TABLE_NAME='one_ledger_audit' THEN
    v_kind='finance.journal_posted';
    v_source=NEW.entry_id;
  ELSE
    RAISE EXCEPTION 'Unsupported operation event producer';
  END IF;
  IF v_kind IS NULL THEN RAISE EXCEPTION 'Invalid event type'; END IF;
  INSERT INTO one_operation_outbox(tenant_id,event_type,source_id)
    VALUES(NEW.tenant_id,v_kind,v_source);
  RETURN NEW;
END $$;

CREATE TRIGGER one_stock_event_outbox AFTER INSERT ON one_stock_events
  FOR EACH ROW EXECUTE FUNCTION one_route_operation_event();
CREATE TRIGGER one_delivery_event_outbox AFTER INSERT ON one_fulfillment_audit
  FOR EACH ROW EXECUTE FUNCTION one_route_operation_event();
CREATE TRIGGER one_receiving_event_outbox AFTER INSERT ON one_receiving_audit
  FOR EACH ROW EXECUTE FUNCTION one_route_operation_event();
CREATE TRIGGER one_invoice_event_outbox AFTER INSERT ON one_invoice_audit
  FOR EACH ROW EXECUTE FUNCTION one_route_operation_event();
CREATE TRIGGER one_ledger_event_outbox AFTER INSERT ON one_ledger_audit
  FOR EACH ROW EXECUTE FUNCTION one_route_operation_event();

REVOKE ALL ON one_operation_outbox FROM PUBLIC;
REVOKE ALL ON FUNCTION one_route_operation_event() FROM PUBLIC;
COMMIT;
