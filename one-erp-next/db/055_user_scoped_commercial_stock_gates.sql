-- ONE ERP NEXT 055 | User-scoped commercial source mutation permissions.
-- Tenant RLS, existing stock/event/return guards and 048 independently
-- approved per-actor/role grants remain authoritative. These BEFORE triggers
-- protect PHYSICAL source mutations even from old stored entrypoints; they do
-- not grant accountant delegation through legacy admin-only functions yet.
-- Trusted backend ONLY: custom PostgreSQL GUCs are NOT authentication.
BEGIN;

INSERT INTO one_permission_templates(permission_key,role,default_allow,risk_class)
SELECT p.key,r.role,r.granted,p.risk
FROM (VALUES
 ('sales.order.confirm','finance'),
 ('purchase.order.confirm','finance'),
 ('inventory.purchase.receive','high'),
 ('inventory.sale.dispatch','high'),
 ('inventory.sale.reserve','finance'),
 ('inventory.sale.release','finance'),
 ('inventory.delivery.prepare','finance'),
 ('sales.return.request','finance'),
 ('purchase.return.request','finance'),
 ('inventory.customer_return.review','high'),
 ('inventory.vendor_return.review','high'),
 ('inventory.return.disposition','high')
) AS p(key,risk)
CROSS JOIN LATERAL (VALUES
 ('company_admin',true),('accountant',false),('viewer',false)
) AS r(role,granted);
INSERT INTO one_permission_keys(permission_key)
SELECT DISTINCT permission_key FROM one_permission_templates
WHERE permission_key LIKE 'sales.%' OR permission_key LIKE 'purchase.%'
 OR permission_key LIKE 'inventory.%'
ON CONFLICT DO NOTHING;

-- Single fail-closed source gate. It does not rely on frontend button states.
-- Every caller must have active membership, matching tenant and actor
-- context, and action-specific permission at the instant of mutation.
CREATE FUNCTION one_source_actor_permitted(
 p_tenant uuid,p_actor uuid,p_permission text
) RETURNS boolean LANGUAGE sql STABLE SECURITY INVOKER AS $actor_allowed$
 SELECT one_actor_has_permission(p_tenant,p_actor,p_permission);
$actor_allowed$;

CREATE FUNCTION one_commercial_source_permission_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $source_gate$
DECLARE action_key text;
 actor uuid;
BEGIN
 IF TG_OP='DELETE' THEN
   -- Older immutable-state triggers also reject DELETE, but don't depend
   -- on their lexical trigger order here.
   RAISE EXCEPTION 'Commercial source events cannot be deleted';
 END IF;
 IF TG_TABLE_NAME='one_commercial_orders' THEN
   IF TG_OP<>'UPDATE' THEN RETURN NEW; END IF;
   IF NEW.status IS DISTINCT FROM OLD.status THEN
      IF NEW.status<>'confirmed' OR OLD.status<>'draft' THEN
         RAISE EXCEPTION 'Unsupported commercial source status transition';
      END IF;
      action_key=CASE NEW.kind WHEN 'sale' THEN 'sales.order.confirm'
        WHEN 'purchase' THEN 'purchase.order.confirm' END;
      actor=NEW.confirmed_by;
   ELSE
      RETURN NEW;
   END IF;
 ELSIF TG_TABLE_NAME='one_stock_events' THEN
   IF TG_OP<>'INSERT' THEN RETURN NEW; END IF;
   actor=NEW.actor_id;
   action_key=CASE NEW.movement_kind
     WHEN 'purchase_receipt' THEN 'inventory.purchase.receive'
     WHEN 'sale_dispatch' THEN 'inventory.sale.dispatch'
     WHEN 'customer_return' THEN 'inventory.customer_return.review'
     WHEN 'vendor_return' THEN 'inventory.vendor_return.review'
     WHEN 'return_restock_out' THEN 'inventory.return.disposition'
     WHEN 'return_restock_in' THEN 'inventory.return.disposition'
     WHEN 'return_disposal' THEN 'inventory.return.disposition'
     ELSE NULL END;
 ELSIF TG_TABLE_NAME='one_reservation_events' THEN
   IF TG_OP<>'INSERT' THEN RETURN NEW; END IF;
   actor=NEW.actor_id;
   action_key=CASE NEW.action
     WHEN 'hold' THEN 'inventory.sale.reserve'
     WHEN 'release' THEN 'inventory.sale.release' END;
 ELSIF TG_TABLE_NAME='one_return_jobs' THEN
   IF TG_OP='INSERT' THEN
     actor=NEW.requested_by;
     action_key=CASE NEW.return_kind
       WHEN 'customer' THEN 'sales.return.request'
       WHEN 'vendor' THEN 'purchase.return.request' END;
   ELSE
     IF OLD.status=NEW.status THEN RETURN NEW; END IF;
     actor=NEW.reviewed_by;
     action_key=CASE NEW.return_kind
       WHEN 'customer' THEN 'inventory.customer_return.review'
       WHEN 'vendor' THEN 'inventory.vendor_return.review' END;
   END IF;
 ELSIF TG_TABLE_NAME='one_return_dispositions' THEN
   IF TG_OP<>'INSERT' THEN RETURN NEW; END IF;
   actor=NEW.actor_id;
   action_key='inventory.return.disposition';
 ELSIF TG_TABLE_NAME='one_delivery_jobs' THEN
   -- Delivery job's stored actor is creator; stage transitions must use
   -- the authenticated *current* actor instead of impersonating creator.
   actor=nullif(current_setting('one.actor_id',true),'')::uuid;
   IF TG_OP='INSERT' OR NEW.status IN ('picked','packed','delivered') THEN
     action_key='inventory.delivery.prepare';
   ELSIF NEW.status='dispatched' THEN
     action_key='inventory.sale.dispatch';
   ELSE
     RAISE EXCEPTION 'Unsupported fulfillment stage';
   END IF;
 ELSIF TG_TABLE_NAME='one_receiving_inspections' THEN
   actor=nullif(current_setting('one.actor_id',true),'')::uuid;
   action_key='inventory.purchase.receive';
 ELSE
   RAISE EXCEPTION 'Unsupported commercial permission target';
 END IF;
 IF action_key IS NULL OR actor IS NULL OR NOT
    one_source_actor_permitted(NEW.tenant_id,actor,action_key) THEN
   RAISE EXCEPTION 'Commercial source operation denied by user permissions';
 END IF;
 RETURN NEW;
END $source_gate$;

CREATE TRIGGER one_permission_order_confirmation
 BEFORE UPDATE OF status ON one_commercial_orders
 FOR EACH ROW EXECUTE FUNCTION one_commercial_source_permission_guard();
CREATE TRIGGER one_permission_stock_event_insert
 BEFORE INSERT ON one_stock_events
 FOR EACH ROW EXECUTE FUNCTION one_commercial_source_permission_guard();
CREATE TRIGGER one_permission_reservation_event
 BEFORE INSERT ON one_reservation_events
 FOR EACH ROW EXECUTE FUNCTION one_commercial_source_permission_guard();
CREATE TRIGGER one_permission_return_request_review
 BEFORE INSERT OR UPDATE OF status ON one_return_jobs
 FOR EACH ROW EXECUTE FUNCTION one_commercial_source_permission_guard();
CREATE TRIGGER one_permission_return_disposition
 BEFORE INSERT ON one_return_dispositions
 FOR EACH ROW EXECUTE FUNCTION one_commercial_source_permission_guard();
CREATE TRIGGER one_permission_delivery_stage
 BEFORE INSERT OR UPDATE OF status ON one_delivery_jobs
 FOR EACH ROW EXECUTE FUNCTION one_commercial_source_permission_guard();
CREATE TRIGGER one_permission_receiving_stage
 BEFORE INSERT OR UPDATE OF status ON one_receiving_inspections
 FOR EACH ROW EXECUTE FUNCTION one_commercial_source_permission_guard();

-- 054 default EXECUTE is revoked from PUBLIC for trusted migration owner.
-- Grant to the broad disposable test runtime separately; production caller
-- permissions need a reviewed *distinct* commercial executor role.
REVOKE EXECUTE ON FUNCTION one_source_actor_permitted(uuid,uuid,text)
 FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION one_commercial_source_permission_guard()
 FROM PUBLIC;
COMMIT;
