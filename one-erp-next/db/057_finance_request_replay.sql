-- ONE ERP NEXT | 057: internal finance same-request replay journal.
-- NOT government e-invoice issuance, a payment gateway, or bank confirmation.
-- MUST be applied by a reviewed migration role, never by a public/browser role.
BEGIN;
CREATE TABLE one_finance_requests (
    tenant_id uuid NOT NULL REFERENCES one_tenants(id),
    operation text NOT NULL CHECK (operation IN ('internal_invoice','internal_payment')),
    request_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    request_sha256 text NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
    result_id uuid CHECK (result_id IS NULL OR result_id=request_id),
    PRIMARY KEY (tenant_id,operation,request_id)
);

ALTER TABLE one_finance_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_finance_requests FORCE ROW LEVEL SECURITY;
CREATE POLICY finance_requests_tenant_scope ON one_finance_requests
    USING (tenant_id::text = current_setting('one.tenant_id',true))
    WITH CHECK (tenant_id::text = current_setting('one.tenant_id',true));

-- A completed request is immutable, including its identity and result.
-- Incomplete requests must not be committed; Python keeps reservation and
-- action within a single transaction, and explicitly refuses stale pending rows.
CREATE FUNCTION one_finance_request_guard() RETURNS trigger LANGUAGE plpgsql AS $
BEGIN
    IF TG_OP='INSERT' THEN
        IF NEW.result_id IS NOT NULL THEN
            RAISE EXCEPTION 'Finance request must start pending';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP='DELETE' OR OLD.result_id IS NOT NULL THEN
        RAISE EXCEPTION 'Completed finance request cannot change';
    END IF;
    IF ROW(NEW.tenant_id, NEW.operation, NEW.request_id, NEW.actor_id, NEW.request_sha256)
       IS DISTINCT FROM
       ROW(OLD.tenant_id, OLD.operation, OLD.request_id, OLD.actor_id, OLD.request_sha256)
       OR NEW.result_id IS NULL THEN
        RAISE EXCEPTION 'Only pending-to-completed result update permitted';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER finance_request_immutable BEFORE INSERT OR UPDATE OR DELETE
    ON one_finance_requests FOR EACH ROW
    EXECUTE FUNCTION one_finance_request_guard();

REVOKE ALL ON TABLE one_finance_requests FROM PUBLIC;
REVOKE ALL ON FUNCTION one_finance_request_guard() FROM PUBLIC;
-- Production runtime SELECT/INSERT/UPDATE grants must be separately reviewed,
-- scoped to a restricted authenticated finance service role; NO browser SQL.
COMMIT;
