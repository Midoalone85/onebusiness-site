-- ONE ERP NEXT 059: immutable, independently approved INTERNAL invoice input.
-- Deploy only under controlled migrations to an isolated tenant database.
-- Browser clients must never receive SQL credentials or INSERT privileges here.
BEGIN;

CREATE TABLE public.one_invoice_source_requests (
    tenant_id uuid NOT NULL REFERENCES public.one_tenants(id),
    document_id uuid NOT NULL,
    period_id uuid NOT NULL,
    requested_by uuid NOT NULL,
    payload jsonb NOT NULL,
    requested_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    PRIMARY KEY (tenant_id, document_id),
    FOREIGN KEY (tenant_id, period_id) REFERENCES public.one_periods(tenant_id,id),
    CHECK (jsonb_typeof(payload) = 'object'),
    CHECK (octet_length(payload::text) BETWEEN 2 AND 32768)
);

CREATE TABLE public.one_invoice_source_approvals (
    tenant_id uuid NOT NULL,
    document_id uuid NOT NULL,
    approved_by uuid NOT NULL,
    approved_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    PRIMARY KEY (tenant_id, document_id),
    FOREIGN KEY (tenant_id, document_id) REFERENCES
       public.one_invoice_source_requests(tenant_id, document_id)
);

ALTER TABLE public.one_invoice_source_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.one_invoice_source_requests FORCE ROW LEVEL SECURITY;
CREATE POLICY one_invoice_source_request_scope ON public.one_invoice_source_requests
    USING (tenant_id::text = current_setting('one.tenant_id',true))
    WITH CHECK (tenant_id::text = current_setting('one.tenant_id',true));

ALTER TABLE public.one_invoice_source_approvals ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.one_invoice_source_approvals FORCE ROW LEVEL SECURITY;
CREATE POLICY one_invoice_source_approval_scope ON public.one_invoice_source_approvals
    USING (tenant_id::text = current_setting('one.tenant_id',true))
    WITH CHECK (tenant_id::text = current_setting('one.tenant_id',true));

CREATE FUNCTION public.one_guard_invoice_source_request()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER
SET search_path = pg_catalog, public AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'Approved source request is append-only';
    END IF;
    IF NEW.requested_by::text IS DISTINCT FROM current_setting('one.actor_id',true)
        OR NEW.tenant_id::text IS DISTINCT FROM current_setting('one.tenant_id',true)
        OR NOT EXISTS (
            SELECT 1 FROM public.one_memberships
            WHERE tenant_id=NEW.tenant_id AND actor_id=NEW.requested_by
              AND active AND role IN ('company_admin','accountant')
        ) THEN
        RAISE EXCEPTION 'Trusted active finance requester required';
    END IF;
    RETURN NEW;
END $$;

CREATE FUNCTION public.one_guard_invoice_source_approval()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER
SET search_path = pg_catalog, public AS $$
DECLARE original_requester uuid;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'Source approval is append-only';
    END IF;
    IF NEW.approved_by::text IS DISTINCT FROM current_setting('one.actor_id',true)
        OR NEW.tenant_id::text IS DISTINCT FROM current_setting('one.tenant_id',true)
        OR NOT EXISTS (
            SELECT 1 FROM public.one_memberships
            WHERE tenant_id=NEW.tenant_id AND actor_id=NEW.approved_by
              AND active AND role IN ('company_admin','accountant')
        ) THEN
        RAISE EXCEPTION 'Trusted active finance approver required';
    END IF;
    SELECT requested_by INTO original_requester
      FROM public.one_invoice_source_requests
      WHERE tenant_id=NEW.tenant_id AND document_id=NEW.document_id
      FOR SHARE;
    IF original_requester IS NULL OR original_requester=NEW.approved_by THEN
        RAISE EXCEPTION 'Independent source approval required';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER one_invoice_source_request_guard
BEFORE INSERT OR UPDATE OR DELETE ON public.one_invoice_source_requests
FOR EACH ROW EXECUTE FUNCTION public.one_guard_invoice_source_request();
CREATE TRIGGER one_invoice_source_approval_guard
BEFORE INSERT OR UPDATE OR DELETE ON public.one_invoice_source_approvals
FOR EACH ROW EXECUTE FUNCTION public.one_guard_invoice_source_approval();

REVOKE ALL ON TABLE public.one_invoice_source_requests FROM PUBLIC;
REVOKE ALL ON TABLE public.one_invoice_source_approvals FROM PUBLIC;
REVOKE ALL ON FUNCTION public.one_guard_invoice_source_request() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.one_guard_invoice_source_approval() FROM PUBLIC;
-- No app-role grants here. DBA must grant the minimum to separately
-- authenticated request/approve and read-only internal-posting roles.
-- A backend that lets callers run raw SQL or forge SET LOCAL is unsafe.
COMMIT;
