-- ONE ERP NEXT 062 | Explicit independent internal invoice reviewer permission.
-- Append-only permission seed; grant application roles separately after DBA review.
-- This is INTERNAL accounting control, not a country tax or ZATCA approval.
BEGIN;

INSERT INTO public.one_permission_templates
    (permission_key,role,default_allow,risk_class)
VALUES
    ('finance.invoice_source.review','company_admin',true,'finance'),
    ('finance.invoice_source.review','accountant',true,'finance'),
    ('finance.invoice_source.review','viewer',false,'finance')
ON CONFLICT(permission_key,role) DO NOTHING;

INSERT INTO public.one_permission_keys(permission_key)
VALUES ('finance.invoice_source.review')
ON CONFLICT(permission_key) DO NOTHING;

CREATE FUNCTION public.one_invoice_approval_permission_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER
SET search_path=pg_catalog,public AS $$
BEGIN
    IF TG_OP <> 'INSERT' OR
       NOT public.one_actor_has_permission(
           NEW.tenant_id,NEW.approved_by,'finance.invoice_source.review')
    THEN
        RAISE EXCEPTION 'Independent invoice review permission denied';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER one_invoice_approval_permission_guard
BEFORE INSERT OR UPDATE OR DELETE ON public.one_invoice_source_approvals
FOR EACH ROW EXECUTE FUNCTION public.one_invoice_approval_permission_guard();

REVOKE ALL ON FUNCTION public.one_invoice_approval_permission_guard() FROM PUBLIC;
-- No blanket DML or PUBLIC grants. Existing invoice approval triggers remain.
COMMIT;
