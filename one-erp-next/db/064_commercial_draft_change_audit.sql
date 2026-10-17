-- ONE ERP NEXT 064 | Append-only source facts for commercial drafts.
-- NOT DEPLOYED. No bulk snapshots, partner fields, secrets or patient data.
-- The audit is written atomically by AFTER triggers, not browser click logs.
-- WARNING: custom GUCs are not authenticated; service SQL must be locked down.
BEGIN;

CREATE TABLE public.one_commercial_change_audit (
    tenant_id uuid NOT NULL REFERENCES public.one_tenants(id),
    event_id uuid NOT NULL DEFAULT gen_random_uuid(),
    order_id uuid NOT NULL,
    kind text NOT NULL CHECK (kind IN ('sale','purchase')),
    action text NOT NULL CHECK (action IN ('created','line_added','line_changed','line_removed')),
    line_no integer,
    actor_id uuid NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    before_values jsonb NOT NULL DEFAULT '{}'::jsonb,
    after_values jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY(tenant_id,event_id),
    FOREIGN KEY(tenant_id,order_id)
        REFERENCES public.one_commercial_orders(tenant_id,id),
    CHECK (jsonb_typeof(before_values)='object' AND
           jsonb_typeof(after_values)='object'),
    CHECK ((action='created' AND line_no IS NULL) OR
           (action<>'created' AND line_no>0))
);
CREATE INDEX one_commercial_change_audit_order_recent
  ON public.one_commercial_change_audit(tenant_id,order_id,occurred_at DESC,event_id DESC);
ALTER TABLE public.one_commercial_change_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.one_commercial_change_audit FORCE ROW LEVEL SECURITY;
CREATE POLICY one_commercial_change_audit_tenant
  ON public.one_commercial_change_audit
  USING(tenant_id::text=current_setting('one.tenant_id',true))
  WITH CHECK(tenant_id::text=current_setting('one.tenant_id',true));

CREATE TRIGGER one_commercial_change_audit_immutable
  BEFORE UPDATE OR DELETE ON public.one_commercial_change_audit
  FOR EACH ROW EXECUTE FUNCTION public.one_protect_audit();

CREATE FUNCTION public.one_capture_commercial_draft_change()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER
SET search_path=pg_catalog,public AS $audit$
DECLARE
  v_tenant uuid;
  v_order uuid;
  v_actor uuid;
  v_kind text;
  v_status text;
  v_action text;
  v_old jsonb := '{}'::jsonb;
  v_new jsonb := '{}'::jsonb;
  v_line integer;
BEGIN
  -- Disposable CI fixture bootstrap uses its privileged migration owner and
  -- deliberately has no signed actor. Real service logins MUST NOT be superusers.
  IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles
             WHERE rolname=current_user AND rolsuper) THEN
    RETURN NULL;
  END IF;
  IF TG_TABLE_NAME='one_commercial_orders' THEN
    IF TG_OP<>'INSERT' THEN RETURN NULL; END IF;
    v_tenant:=NEW.tenant_id; v_order:=NEW.id; v_kind:=NEW.kind;
    v_action:='created';
    v_new:=jsonb_build_object('status',NEW.status);
  ELSIF TG_TABLE_NAME='one_commercial_order_lines' THEN
    IF TG_OP='DELETE' THEN
      v_tenant:=OLD.tenant_id; v_order:=OLD.order_id; v_line:=OLD.line_no;
      v_old:=jsonb_build_object('line_no',OLD.line_no,
        'product_id',OLD.product_id::text,'warehouse_id',OLD.warehouse_id::text,
        'ordered_qty',OLD.ordered_qty::text,'unit_price',OLD.unit_price::text);
      v_action:='line_removed';
    ELSE
      v_tenant:=NEW.tenant_id; v_order:=NEW.order_id; v_line:=NEW.line_no;
      v_new:=jsonb_build_object('line_no',NEW.line_no,
        'product_id',NEW.product_id::text,'warehouse_id',NEW.warehouse_id::text,
        'ordered_qty',NEW.ordered_qty::text,'unit_price',NEW.unit_price::text);
      IF TG_OP='INSERT' THEN
        v_action:='line_added';
      ELSE
        IF ROW(NEW.product_id,NEW.warehouse_id,NEW.ordered_qty,NEW.unit_price)
           IS NOT DISTINCT FROM
           ROW(OLD.product_id,OLD.warehouse_id,OLD.ordered_qty,OLD.unit_price) THEN
           RETURN NULL;
        END IF;
        v_old:=jsonb_build_object('line_no',OLD.line_no,
          'product_id',OLD.product_id::text,'warehouse_id',OLD.warehouse_id::text,
          'ordered_qty',OLD.ordered_qty::text,'unit_price',OLD.unit_price::text);
        v_action:='line_changed';
      END IF;
    END IF;
    SELECT o.kind,o.status INTO v_kind,v_status
      FROM public.one_commercial_orders o
     WHERE o.tenant_id=v_tenant AND o.id=v_order;
    IF v_kind IS NULL THEN RAISE EXCEPTION 'Source order missing for audit'; END IF;
    -- Confirmed-line fulfillment belongs to the stock event audit, not draft changes.
    IF v_status<>'draft' THEN RETURN NULL; END IF;
  ELSE
    RAISE EXCEPTION 'Unsupported commercial audit source';
  END IF;

  IF current_setting('one.tenant_id',true) IS DISTINCT FROM v_tenant::text THEN
    RAISE EXCEPTION 'Tenant activity context required';
  END IF;
  v_actor:=nullif(current_setting('one.actor_id',true),'')::uuid;
  IF v_actor IS NULL OR v_actor='00000000-0000-0000-0000-000000000000'::uuid OR
     NOT EXISTS (SELECT 1 FROM public.one_memberships m
       WHERE m.tenant_id=v_tenant AND m.actor_id=v_actor AND m.active) THEN
    RAISE EXCEPTION 'Verified actor membership required for activity capture';
  END IF;
  INSERT INTO public.one_commercial_change_audit
    (tenant_id,order_id,kind,action,line_no,actor_id,before_values,after_values)
  VALUES(v_tenant,v_order,v_kind,v_action,v_line,v_actor,v_old,v_new);
  RETURN NULL;
END;
$audit$;
CREATE TRIGGER one_commercial_order_created_audit
  AFTER INSERT ON public.one_commercial_orders
  FOR EACH ROW EXECUTE FUNCTION public.one_capture_commercial_draft_change();
CREATE TRIGGER one_commercial_order_line_change_audit
  AFTER INSERT OR UPDATE OR DELETE ON public.one_commercial_order_lines
  FOR EACH ROW EXECUTE FUNCTION public.one_capture_commercial_draft_change();

REVOKE ALL ON public.one_commercial_change_audit FROM PUBLIC;
REVOKE ALL ON FUNCTION public.one_capture_commercial_draft_change() FROM PUBLIC;
-- Production deployment must grant only necessary table INSERT to a dedicated
-- restricted source writer, SELECT to a dedicated activity reader, and no DML
-- on audit facts. Test-only broad grants are not production templates.
COMMIT;
