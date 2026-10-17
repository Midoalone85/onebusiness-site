-- ONE ERP NEXT 060: maker-authorized, reviewer-attested commercial invoice
-- intent plus immutable allocation lineage. Internal ledger only, NOT ZATCA.
-- Financial policy/tax treatment still requires independent country pack.
BEGIN;

CREATE TABLE public.one_sales_invoice_intents (
    tenant_id uuid NOT NULL REFERENCES public.one_tenants(id),
    invoice_id uuid NOT NULL,
    order_id uuid NOT NULL,
    period_id uuid NOT NULL,
    number text NOT NULL CHECK(length(btrim(number)) BETWEEN 1 AND 100),
    issued_on date NOT NULL,
    due_on date NOT NULL CHECK(due_on >= issued_on),
    receivable_account_id text NOT NULL,
    payable_account_id text NOT NULL,
    revenue_account_id text NOT NULL,
    output_vat_account_id text NOT NULL,
    input_vat_account_id text NOT NULL,
    tax_category text NOT NULL CHECK(tax_category IN
        ('standard','zero_rated','exempt','out_of_scope')),
    tax_rate numeric(8,6) NOT NULL CHECK(tax_rate >= 0 AND tax_rate <= 1),
    tax_code text NOT NULL CHECK(length(btrim(tax_code)) BETWEEN 1 AND 64),
    tax_policy_reference text NOT NULL CHECK(
        length(btrim(tax_policy_reference)) BETWEEN 6 AND 160),
    tax_policy_reviewed_by uuid NOT NULL,
    requested_by uuid NOT NULL,
    requested_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    PRIMARY KEY (tenant_id, invoice_id),
    UNIQUE (tenant_id, number),
    FOREIGN KEY (tenant_id,order_id)
        REFERENCES public.one_commercial_orders(tenant_id,id),
    FOREIGN KEY (tenant_id,period_id)
        REFERENCES public.one_periods(tenant_id,id),
    FOREIGN KEY (tenant_id,receivable_account_id)
        REFERENCES public.one_accounts(tenant_id,id),
    FOREIGN KEY (tenant_id,payable_account_id)
        REFERENCES public.one_accounts(tenant_id,id),
    FOREIGN KEY (tenant_id,revenue_account_id)
        REFERENCES public.one_accounts(tenant_id,id),
    FOREIGN KEY (tenant_id,output_vat_account_id)
        REFERENCES public.one_accounts(tenant_id,id),
    FOREIGN KEY (tenant_id,input_vat_account_id)
        REFERENCES public.one_accounts(tenant_id,id),
    CHECK(tax_policy_reviewed_by <> requested_by),
    CHECK(
        (tax_category='standard' AND tax_rate > 0 AND tax_code <> 'NONE')
        OR (tax_category<>'standard' AND tax_rate=0)
    )
);

ALTER TABLE public.one_sales_invoice_intents ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.one_sales_invoice_intents FORCE ROW LEVEL SECURITY;
CREATE POLICY one_sales_invoice_intents_scope
ON public.one_sales_invoice_intents
USING (tenant_id::text=current_setting('one.tenant_id',true))
WITH CHECK (tenant_id::text=current_setting('one.tenant_id',true));

CREATE FUNCTION public.one_sales_invoice_intent_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER
SET search_path=pg_catalog,public AS $$
BEGIN
  IF TG_OP <> 'INSERT' THEN
    RAISE EXCEPTION 'Sale invoice intent is append-only';
  END IF;
  IF NEW.requested_by::text IS DISTINCT FROM current_setting('one.actor_id',true)
     OR NEW.tenant_id::text IS DISTINCT FROM current_setting('one.tenant_id',true)
     OR NOT EXISTS (
       SELECT 1 FROM public.one_memberships
       WHERE tenant_id=NEW.tenant_id AND actor_id=NEW.requested_by
         AND active AND role IN ('accountant','company_admin'))
     OR NOT EXISTS (
       SELECT 1 FROM public.one_memberships
       WHERE tenant_id=NEW.tenant_id AND actor_id=NEW.tax_policy_reviewed_by
         AND active AND role IN ('accountant','company_admin'))
  THEN
     RAISE EXCEPTION 'Authenticated maker and separate active tax reviewer required';
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER one_sales_invoice_intent_guard
BEFORE INSERT OR UPDATE OR DELETE ON public.one_sales_invoice_intents
FOR EACH ROW EXECUTE FUNCTION public.one_sales_invoice_intent_guard();

-- Bridge a source snapshot to its precise order lines.
-- The matching source must exist in the same approved-source transaction.
CREATE TABLE public.one_sales_invoice_source_links (
    tenant_id uuid NOT NULL,
    invoice_id uuid NOT NULL,
    order_id uuid NOT NULL,
    order_line_nos integer[] NOT NULL,
    actor_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    PRIMARY KEY (tenant_id,invoice_id),
    FOREIGN KEY (tenant_id,invoice_id)
        REFERENCES public.one_invoice_source_requests(tenant_id,document_id),
    FOREIGN KEY (tenant_id,order_id)
        REFERENCES public.one_commercial_orders(tenant_id,id),
    CHECK (coalesce(cardinality(order_line_nos),0) BETWEEN 1 AND 100),
    CHECK (array_position(order_line_nos,NULL) IS NULL)
);
ALTER TABLE public.one_sales_invoice_source_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.one_sales_invoice_source_links FORCE ROW LEVEL SECURITY;
CREATE POLICY one_sales_invoice_source_links_scope
ON public.one_sales_invoice_source_links
USING (tenant_id::text=current_setting('one.tenant_id',true))
WITH CHECK (tenant_id::text=current_setting('one.tenant_id',true));

CREATE FUNCTION public.one_sales_invoice_source_link_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER
SET search_path=pg_catalog,public AS $$
BEGIN
  IF TG_OP <> 'INSERT' THEN
    RAISE EXCEPTION 'Sale invoice source lineage is append-only';
  END IF;
  IF NEW.actor_id::text IS DISTINCT FROM current_setting('one.actor_id',true)
     OR NEW.tenant_id::text IS DISTINCT FROM current_setting('one.tenant_id',true)
     OR NOT EXISTS (
       SELECT 1 FROM public.one_invoice_source_requests s
       WHERE s.tenant_id=NEW.tenant_id AND s.document_id=NEW.invoice_id
         AND s.requested_by=NEW.actor_id)
     OR EXISTS (SELECT 1 FROM unnest(NEW.order_line_nos) AS x(v)
                WHERE x.v IS NULL OR x.v<=0)
     OR (SELECT count(DISTINCT v) FROM unnest(NEW.order_line_nos) AS x(v))
        <> cardinality(NEW.order_line_nos)
  THEN
     RAISE EXCEPTION 'Immutable authenticated sales source lineage required';
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER one_sales_invoice_source_link_guard
BEFORE INSERT OR UPDATE OR DELETE ON public.one_sales_invoice_source_links
FOR EACH ROW EXECUTE FUNCTION public.one_sales_invoice_source_link_guard();

REVOKE ALL ON TABLE public.one_sales_invoice_intents FROM PUBLIC;
REVOKE ALL ON TABLE public.one_sales_invoice_source_links FROM PUBLIC;
REVOKE ALL ON FUNCTION public.one_sales_invoice_intent_guard() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.one_sales_invoice_source_link_guard() FROM PUBLIC;
-- Roles/grants are NOT provisioned by this migration.
-- Production country packs, tax issuance and tested application roles are pending.
COMMIT;
