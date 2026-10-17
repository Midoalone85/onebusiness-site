-- ONE ERP Next: source-safe sales invoice allocations.
-- CI/development only. This is not tax approval, ZATCA or live ERP issuance.
-- A trusted backend must authenticate and choose the tenant DB before SQL.
BEGIN;

CREATE TABLE one_sales_invoice_allocations (
    tenant_id uuid NOT NULL,
    invoice_id uuid NOT NULL,
    invoice_line_number integer NOT NULL CHECK (invoice_line_number > 0),
    order_id uuid NOT NULL,
    order_line_no integer NOT NULL CHECK (order_line_no > 0),
    billed_qty numeric(20,6) NOT NULL CHECK (billed_qty > 0),
    actor_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    PRIMARY KEY (tenant_id, invoice_id, invoice_line_number),
    FOREIGN KEY (tenant_id, invoice_id, invoice_line_number)
        REFERENCES one_invoice_lines(tenant_id,doc_id,line_number),
    FOREIGN KEY (tenant_id, order_id, order_line_no)
        REFERENCES one_commercial_order_lines(tenant_id,order_id,line_no)
);
CREATE INDEX one_sale_invoice_by_order_line
    ON one_sales_invoice_allocations(tenant_id,order_id,order_line_no);
ALTER TABLE one_sales_invoice_allocations ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_sales_invoice_allocations FORCE ROW LEVEL SECURITY;
CREATE POLICY one_sale_invoice_alloc_scope ON one_sales_invoice_allocations
    USING (tenant_id::text=current_setting('one.tenant_id',true))
    WITH CHECK (tenant_id::text=current_setting('one.tenant_id',true));
CREATE TRIGGER one_sales_invoice_allocations_immutable
    BEFORE UPDATE OR DELETE ON one_sales_invoice_allocations FOR EACH ROW
    EXECUTE FUNCTION one_stock_event_immutable();

CREATE FUNCTION one_link_sales_invoice(
    p_tenant uuid, p_invoice uuid, p_order uuid, p_actor uuid,
    p_order_line_nos integer[]
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE
    o one_commercial_orders%ROWTYPE;
    doc one_invoice_docs%ROWTYPE;
    src one_commercial_order_lines%ROWTYPE;
    inv one_invoice_lines%ROWTYPE;
    index_no integer;
    existing_qty numeric(24,6);
    n integer;
BEGIN
    IF p_tenant IS NULL OR p_invoice IS NULL OR p_order IS NULL OR
       p_actor IS NULL OR p_order_line_nos IS NULL OR
       coalesce(cardinality(p_order_line_nos),0)=0 OR
       cardinality(p_order_line_nos)>1000 OR
       current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
       current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
       NOT EXISTS (
           SELECT 1 FROM one_memberships
           WHERE tenant_id=p_tenant AND actor_id=p_actor AND active
             AND role IN ('company_admin','accountant')
       ) THEN
        RAISE EXCEPTION 'Authorized company invoicing context required';
    END IF;
    IF EXISTS (SELECT 1 FROM unnest(p_order_line_nos) AS x(v)
               WHERE v IS NULL OR v <= 0) OR
       (SELECT count(DISTINCT v) FROM unnest(p_order_line_nos) AS x(v))
           <> cardinality(p_order_line_nos) THEN
        RAISE EXCEPTION 'Invoice source order lines must be positive and unique';
    END IF;
    -- Serialize all billing checks for one order across concurrent invoices.
    SELECT * INTO o FROM one_commercial_orders
       WHERE tenant_id=p_tenant AND id=p_order FOR UPDATE;
    IF NOT FOUND OR o.kind<>'sale' OR o.status<>'confirmed' THEN
        RAISE EXCEPTION 'Confirmed sales order required for invoice';
    END IF;
    SELECT * INTO doc FROM one_invoice_docs
       WHERE tenant_id=p_tenant AND id=p_invoice FOR UPDATE;
    IF NOT FOUND OR doc.kind<>'customer_invoice' OR doc.status<>'issued'
       OR doc.partner_id<>o.partner_id OR doc.currency<>o.currency THEN
        RAISE EXCEPTION 'Issued customer invoice must match order partner and currency';
    END IF;
    SELECT count(*) INTO n FROM one_invoice_lines
       WHERE tenant_id=p_tenant AND doc_id=p_invoice;
    IF n<>cardinality(p_order_line_nos) THEN
        RAISE EXCEPTION 'Every invoice line must be matched to an order line';
    END IF;
    IF EXISTS(SELECT 1 FROM one_sales_invoice_allocations
              WHERE tenant_id=p_tenant AND invoice_id=p_invoice) THEN
        RAISE EXCEPTION 'Invoice has already been source-linked';
    END IF;
    FOR index_no IN 1..cardinality(p_order_line_nos) LOOP
        SELECT * INTO src FROM one_commercial_order_lines
            WHERE tenant_id=p_tenant AND order_id=p_order
              AND line_no=p_order_line_nos[index_no] FOR UPDATE;
        IF NOT FOUND THEN RAISE EXCEPTION 'Sales order line not found'; END IF;
        SELECT * INTO inv FROM one_invoice_lines
            WHERE tenant_id=p_tenant AND doc_id=p_invoice
              AND line_number=index_no;
        IF NOT FOUND OR inv.quantity<>round(inv.quantity,3)
           OR inv.unit_price<>src.unit_price THEN
            RAISE EXCEPTION 'Invoice source line quantity or unit price mismatch';
        END IF;
        SELECT coalesce(sum(billed_qty),0) INTO existing_qty
           FROM one_sales_invoice_allocations
           WHERE tenant_id=p_tenant AND order_id=p_order
             AND order_line_no=src.line_no;
        IF existing_qty+inv.quantity>src.fulfilled_qty THEN
            RAISE EXCEPTION 'Cannot invoice more than dispatched quantity';
        END IF;
        INSERT INTO one_sales_invoice_allocations(
            tenant_id,invoice_id,invoice_line_number,order_id,order_line_no,
            billed_qty,actor_id
        ) VALUES (
            p_tenant,p_invoice,index_no,p_order,src.line_no,inv.quantity,p_actor
        );
    END LOOP;
    RETURN p_invoice;
END $$;

REVOKE ALL ON one_sales_invoice_allocations FROM PUBLIC;
REVOKE ALL ON FUNCTION
    one_link_sales_invoice(uuid,uuid,uuid,uuid,integer[]) FROM PUBLIC;
COMMIT;
