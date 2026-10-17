-- ONE ERP Next 015 | FIRST-CUT STORED WEIGHTED AVERAGE VALUE LEDGER
-- Isolated PostgreSQL CI prototype. Do NOT grant to any production user.
-- Only purchase_receipt and sale_dispatch stock movements are supported.
-- Source price is the contracted PO price, NOT approved landed cost yet.
BEGIN;

CREATE TABLE one_stock_value_positions (
    tenant_id uuid NOT NULL,
    warehouse_id uuid NOT NULL,
    product_id uuid NOT NULL,
    currency text NOT NULL REFERENCES one_currency_precision(code),
    qty numeric(20,3) NOT NULL DEFAULT 0 CHECK (qty>=0),
    book_value numeric(20,3) NOT NULL DEFAULT 0 CHECK (book_value>=0),
    PRIMARY KEY(tenant_id,warehouse_id,product_id),
    FOREIGN KEY(tenant_id,warehouse_id,product_id)
      REFERENCES one_stock_balances(tenant_id,warehouse_id,product_id),
    CHECK ((qty=0 AND book_value=0) OR (qty>0 AND book_value>0))
);
ALTER TABLE one_stock_value_positions ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_stock_value_positions FORCE ROW LEVEL SECURITY;
CREATE POLICY one_stock_value_pos_scope ON one_stock_value_positions
  USING(tenant_id::text=current_setting('one.tenant_id',true))
  WITH CHECK(tenant_id::text=current_setting('one.tenant_id',true));

CREATE TABLE one_stock_value_events (
    tenant_id uuid NOT NULL,
    stock_event_id uuid NOT NULL,
    journal_id uuid NOT NULL,
    movement_kind text NOT NULL CHECK(movement_kind IN ('purchase_receipt','sale_dispatch')),
    qty numeric(20,3) NOT NULL CHECK(qty>0),
    amount numeric(20,3) NOT NULL CHECK(amount>0),
    currency text NOT NULL REFERENCES one_currency_precision(code),
    position_qty_after numeric(20,3) NOT NULL CHECK(position_qty_after>=0),
    position_value_after numeric(20,3) NOT NULL CHECK(position_value_after>=0),
    actor_id uuid NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    PRIMARY KEY(tenant_id,stock_event_id),
    UNIQUE(tenant_id,journal_id),
    FOREIGN KEY(tenant_id,stock_event_id)
      REFERENCES one_stock_events(tenant_id,id),
    FOREIGN KEY(tenant_id,journal_id)
      REFERENCES one_journal_entries(tenant_id,id)
);
ALTER TABLE one_stock_value_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_stock_value_events FORCE ROW LEVEL SECURITY;
CREATE POLICY one_stock_value_event_scope ON one_stock_value_events
  USING(tenant_id::text=current_setting('one.tenant_id',true))
  WITH CHECK(tenant_id::text=current_setting('one.tenant_id',true));
CREATE TRIGGER one_stock_value_event_immutable BEFORE UPDATE OR DELETE
 ON one_stock_value_events FOR EACH ROW EXECUTE FUNCTION one_stock_event_immutable();

CREATE FUNCTION one_stock_value_position_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' OR
    current_setting('one.valuation_context',true) IS DISTINCT FROM 'yes' THEN
    RAISE EXCEPTION 'Stored inventory value needs an approved valuation transaction';
 END IF;
 IF TG_OP='UPDATE' AND ROW(NEW.tenant_id,NEW.warehouse_id,NEW.product_id,NEW.currency)
   IS DISTINCT FROM ROW(OLD.tenant_id,OLD.warehouse_id,OLD.product_id,OLD.currency) THEN
    RAISE EXCEPTION 'Cannot change valuation bucket or its currency';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER one_stock_value_position_guard
 BEFORE INSERT OR UPDATE OR DELETE ON one_stock_value_positions
 FOR EACH ROW EXECUTE FUNCTION one_stock_value_position_guard();

CREATE FUNCTION one_value_physical_stock_event(
  p_tenant uuid, p_source uuid, p_period uuid,
  p_inventory_acct text, p_offset_acct text, p_journal uuid, p_actor uuid
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE
  ev one_stock_events%ROWTYPE;
  l one_commercial_order_lines%ROWTYPE;
  o one_commercial_orders%ROWTYPE;
  sb one_stock_balances%ROWTYPE;
  oldpos one_stock_value_positions%ROWTYPE;
  scale integer;
  monetary numeric(20,3);
  afterqty numeric(20,3);
  afterval numeric(20,3);
  acctkind text;
  offsetkind text;
  unvalued bigint;
  source_order uuid;
BEGIN
 IF p_tenant IS NULL OR p_source IS NULL OR p_period IS NULL OR
    p_inventory_acct IS NULL OR p_offset_acct IS NULL OR
    p_inventory_acct=p_offset_acct OR p_journal IS NULL OR
    p_actor IS NULL OR p_source=p_journal
    OR current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text
    OR current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text
    OR NOT EXISTS (SELECT 1 FROM one_memberships m
      WHERE m.tenant_id=p_tenant AND m.actor_id=p_actor AND m.active
        AND m.role='company_admin') THEN
    RAISE EXCEPTION 'Valid tenant admin and distinct stock journal required';
 END IF;
 -- Standard lock hierarchy: order -> stock bucket -> valuation bucket.
 SELECT order_id INTO source_order FROM one_stock_events
  WHERE tenant_id=p_tenant AND id=p_source;
 IF NOT FOUND THEN RAISE EXCEPTION 'Physical stock source not found'; END IF;
 SELECT * INTO o FROM one_commercial_orders
  WHERE tenant_id=p_tenant AND id=source_order FOR UPDATE;
 SELECT * INTO ev FROM one_stock_events
  WHERE tenant_id=p_tenant AND id=p_source FOR UPDATE;
 IF NOT FOUND OR ev.movement_kind NOT IN ('purchase_receipt','sale_dispatch') THEN
  RAISE EXCEPTION 'Only priced receipt / original sale dispatch eligible for valuation';
 END IF;
 IF (ev.movement_kind='purchase_receipt' AND o.kind<>'purchase') OR
    (ev.movement_kind='sale_dispatch' AND o.kind<>'sale') THEN
  RAISE EXCEPTION 'Physical stock direction conflicts with commercial order';
 END IF;
 SELECT * INTO l FROM one_commercial_order_lines
  WHERE tenant_id=p_tenant AND order_id=ev.order_id AND line_no=ev.line_no;
 IF NOT FOUND OR l.warehouse_id<>ev.warehouse_id OR l.product_id<>ev.product_id
    OR l.unit_price<=0 THEN
  RAISE EXCEPTION 'Invalid stock cost source and immutable commercial line';
 END IF;
 SELECT * INTO sb FROM one_stock_balances
  WHERE tenant_id=p_tenant AND warehouse_id=ev.warehouse_id
   AND product_id=ev.product_id FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'Missing physical stock bucket'; END IF;
 -- Fail CLOSED if another physical event has occurred before valuation. Prevent
 -- posting historical valuations based on today's unit count in this v1 slice.
 SELECT count(*) INTO unvalued FROM one_stock_events x
  WHERE x.tenant_id=p_tenant AND x.warehouse_id=ev.warehouse_id
   AND x.product_id=ev.product_id AND x.id<>p_source
   AND NOT EXISTS(SELECT 1 FROM one_stock_value_events v
       WHERE v.tenant_id=x.tenant_id AND v.stock_event_id=x.id);
 IF unvalued<>0 THEN
  RAISE EXCEPTION 'Unvalued physical movements: perform valuation with each stock event';
 END IF;
 IF EXISTS(SELECT 1 FROM one_stock_value_events
          WHERE tenant_id=p_tenant AND stock_event_id=p_source) THEN
  RAISE EXCEPTION 'Stock event has already been valued';
 END IF;
 SELECT minor_units INTO scale FROM one_currency_precision WHERE code=o.currency;
 IF scale IS NULL OR l.unit_price<>round(l.unit_price,scale) THEN
  RAISE EXCEPTION 'Stock unit cost and currency precision unsupported';
 END IF;
 SELECT kind INTO acctkind FROM one_accounts WHERE tenant_id=p_tenant
  AND id=p_inventory_acct AND active AND posting_allowed;
 SELECT kind INTO offsetkind FROM one_accounts WHERE tenant_id=p_tenant
  AND id=p_offset_acct AND active AND posting_allowed;
 IF acctkind IS DISTINCT FROM 'asset' OR
    (ev.movement_kind='purchase_receipt' AND offsetkind IS DISTINCT FROM 'liability') OR
    (ev.movement_kind='sale_dispatch' AND offsetkind IS DISTINCT FROM 'expense') THEN
  RAISE EXCEPTION 'Incorrect inventory/GRNI/COGS account types';
 END IF;
 PERFORM set_config('one.valuation_context','yes',true);
 INSERT INTO one_stock_value_positions(tenant_id,warehouse_id,product_id,currency)
  VALUES(p_tenant,ev.warehouse_id,ev.product_id,o.currency)
  ON CONFLICT (tenant_id,warehouse_id,product_id) DO NOTHING;
 SELECT * INTO oldpos FROM one_stock_value_positions
  WHERE tenant_id=p_tenant AND warehouse_id=ev.warehouse_id
   AND product_id=ev.product_id FOR UPDATE;
 IF oldpos.currency<>o.currency THEN
  RAISE EXCEPTION 'Multi-currency stock bucket requires explicit revaluation';
 END IF;
 IF ev.movement_kind='purchase_receipt' THEN
  afterqty=oldpos.qty+ev.quantity;
  monetary=round(ev.quantity*l.unit_price,scale);
  afterval=oldpos.book_value+monetary;
 ELSE
  IF oldpos.qty<ev.quantity OR oldpos.qty=0 THEN
   RAISE EXCEPTION 'Cannot value a sale before stock receipts';
  END IF;
  afterqty=oldpos.qty-ev.quantity;
  monetary=CASE WHEN afterqty=0 THEN oldpos.book_value
    ELSE round(oldpos.book_value*ev.quantity/oldpos.qty,scale) END;
  afterval=oldpos.book_value-monetary;
 END IF;
 IF monetary<=0 OR afterval<0 OR
    (afterqty=0 AND afterval<>0) OR (afterqty>0 AND afterval<=0) THEN
   RAISE EXCEPTION 'Non-positive/rounding stranded inventory value rejected';
 END IF;
 -- The physical warehouse must be *exactly* after the recorded source event.
 IF afterqty<>sb.on_hand THEN
  RAISE EXCEPTION 'Physical quantity differs from valued event: stale stock snapshot';
 END IF;
 UPDATE one_stock_value_positions SET qty=afterqty,book_value=afterval
  WHERE tenant_id=p_tenant AND warehouse_id=ev.warehouse_id
   AND product_id=ev.product_id;
 INSERT INTO one_journal_entries
   (tenant_id,id,period_id,reference,idempotency_key,occurred_on,currency)
 VALUES(p_tenant,p_journal,p_period,'stock-valued:'||p_source::text,
        'stock-valued:'||p_source::text,ev.created_at::date,o.currency);
 IF ev.movement_kind='purchase_receipt' THEN
  INSERT INTO one_journal_lines
    (tenant_id,entry_id,line_number,account_id,debit,credit)
  VALUES(p_tenant,p_journal,1,p_inventory_acct,monetary,0),
        (p_tenant,p_journal,2,p_offset_acct,0,monetary);
 ELSE
  INSERT INTO one_journal_lines
    (tenant_id,entry_id,line_number,account_id,debit,credit)
  VALUES(p_tenant,p_journal,1,p_offset_acct,monetary,0),
        (p_tenant,p_journal,2,p_inventory_acct,0,monetary);
 END IF;
 PERFORM one_post_journal(p_tenant,p_journal,p_actor);
 INSERT INTO one_stock_value_events(
   tenant_id,stock_event_id,journal_id,movement_kind,qty,amount,currency,
   position_qty_after,position_value_after,actor_id
 ) VALUES(p_tenant,p_source,p_journal,ev.movement_kind,ev.quantity,monetary,
          o.currency,afterqty,afterval,p_actor);
 RETURN p_journal;
END $$;

REVOKE ALL ON FUNCTION
 one_value_physical_stock_event(uuid,uuid,uuid,text,text,uuid,uuid) FROM PUBLIC;
REVOKE ALL ON one_stock_value_positions,one_stock_value_events FROM PUBLIC;
COMMIT;
