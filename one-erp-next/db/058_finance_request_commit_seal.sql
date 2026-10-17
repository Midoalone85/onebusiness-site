-- ONE ERP NEXT 058 | Reject committed finance request reservations lacking a result.
-- Draft migration only: apply after 057 on a reviewed disposable/staging DB.
-- A pending reservation is valid only inside the same active transaction in
-- which the trusted backend writes its internal financial record and seals it.
BEGIN;

CREATE FUNCTION public.one_finance_request_commit_seal()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, public
AS $$
DECLARE
    sealed_result uuid;
BEGIN
    SELECT result_id INTO sealed_result
    FROM public.one_finance_requests
    WHERE tenant_id = NEW.tenant_id
      AND operation = NEW.operation
      AND request_id = NEW.request_id;

    IF sealed_result IS DISTINCT FROM NEW.request_id THEN
        RAISE EXCEPTION 'Incomplete finance request cannot commit'
            USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END
$$;

CREATE CONSTRAINT TRIGGER finance_request_commit_seal
AFTER INSERT ON public.one_finance_requests
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION public.one_finance_request_commit_seal();

REVOKE ALL ON FUNCTION public.one_finance_request_commit_seal() FROM PUBLIC;
COMMIT;
