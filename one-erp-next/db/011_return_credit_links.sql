-- ONE ERP Next: customer-return-backed INTERNAL customer credits.
-- PostgreSQL 14 disposable CI ONLY; no ZATCA issuance / cash refund / live API.
BEGIN;
ALTER TABLE one_operation_outbox DROP CONSTRAINT one_operation_outbox_event_type_check;
ALTER TABLE one_operation_outbox ADD CONSTRAINT one_operation_outbox_event_type_check
 CHECK (event_type IN (
   'stock.purchase_received','stock.sale_dispatched',
   'stock.customer_returned','stock.vendor_returned',
   'delivery.created','delivery.picked','delivery.packed',
   'delivery.dispatched','delivery.delivered',
   'receiving.inspection_started','receiving.inspection_completed',
   'returns.requested','returns.completed','returns.rejected',
   'finance.invoice_issued','finance.journal_posted',
   'finance.customer_return_credited'
 ));

CREATE TABLE one_customer_return_credits (
  tenant_id uuid NOT NULL,
  credit_invoice_id uuid NOT NULL,
  original_invoice_id uuid NOT NULL,
  return_job_id uuid NOT NULL,
  quantity numeric(20,3) NOT NULL CHECK (quantity>0 AND quantity::text<>'NaN'),
  actor_id uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
  PRIMARY KEY(tenant_id,credit_invoice_id),
  FOREIGN KEY(tenant_id,credit_invoice_id)
    REFERENCES one_invoice_docs(tenant_id,id),
  FOREIGN KEY(tenant_id,original_invoice_id)
    REFERENCES one_invoice_docs(tenant_id,id),
  FOREIGN KEY(tenant_id,return_job_id)
    REFERENCES one_return_jobs(tenant_id,id)
);
CREATE INDEX one_customer_return_credit_by_job
  ON one_customer_return_credits(tenant_id,return_job_id);
CREATE INDEX one_customer_return_credit_by_original
  ON one_customer_return_credits(tenant_id,original_invoice_id);
ALTER TABLE one_customer_return_credits ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_customer_return_credits FORCE ROW LEVEL SECURITY;
CREATE POLICY one_return_credit_scope ON one_customer_return_credits
  USING (tenant_id::text=current_setting('one.tenant_id',true))
  WITH CHECK (tenant_id::text=current_setting('one.tenant_id',true));
CREATE TRIGGER one_customer_return_credit_immutable BEFORE UPDATE OR DELETE
  ON one_customer_return_credits FOR EACH ROW EXECUTE FUNCTION one_protect_audit();

CREATE FUNCTION one_customer_return_credit_outbox() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER AS $$
BEGIN
  INSERT INTO one_operation_outbox(tenant_id,event_type,source_id)
    VALUES(NEW.tenant_id,'finance.customer_return_credited',NEW.credit_invoice_id);
  RETURN NEW;
END $$;
CREATE TRIGGER one_customer_return_credit_outbox AFTER INSERT
  ON one_customer_return_credits FOR EACH ROW
  EXECUTE FUNCTION one_customer_return_credit_outbox();

CREATE FUNCTION one_link_customer_return_credit(
  p_tenant uuid,p_credit uuid,p_original uuid,p_return uuid,p_actor uuid
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE j one_return_jobs%ROWTYPE;
        movement one_stock_events%ROWTYPE;
        original_doc one_invoice_docs%ROWTYPE;
        credit_doc one_invoice_docs%ROWTYPE;
        original_line one_invoice_lines%ROWTYPE;
        credit_line one_invoice_lines%ROWTYPE;
        matched_qty numeric(20,6);
        claimed_by_job numeric(24,6);
        claimed_from_original numeric(24,6);
BEGIN
  IF p_tenant IS NULL OR p_credit IS NULL OR p_original IS NULL
     OR p_return IS NULL OR p_actor IS NULL OR
     current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text
     OR current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text
     OR NOT EXISTS(SELECT 1 FROM one_memberships
         WHERE tenant_id=p_tenant AND actor_id=p_actor AND active
           AND role IN ('company_admin','accountant')) THEN
    RAISE EXCEPTION 'Authorized finance user required for return credit link';
  END IF;
  -- Lock accepted return before adding any new credit for this job.
  SELECT * INTO j FROM one_return_jobs WHERE tenant_id=p_tenant AND id=p_return
    FOR UPDATE;
  IF NOT FOUND OR j.return_kind<>'customer' OR j.status<>'completed'
     OR j.accepted_qty<=0 THEN
    RAISE EXCEPTION 'Accepted customer return required';
  END IF;
  SELECT * INTO movement FROM one_stock_events
    WHERE tenant_id=p_tenant AND id=j.source_event_id;
  IF NOT FOUND OR movement.movement_kind<>'sale_dispatch' THEN
    RAISE EXCEPTION 'Customer return must reference a dispatch event';
  END IF;
  SELECT * INTO original_doc FROM one_invoice_docs
    WHERE tenant_id=p_tenant AND id=p_original FOR UPDATE;
  IF NOT FOUND OR original_doc.status<>'issued'
     OR original_doc.kind<>'customer_invoice' THEN
    RAISE EXCEPTION 'Issued original customer invoice required';
  END IF;
  SELECT * INTO credit_doc FROM one_invoice_docs
    WHERE tenant_id=p_tenant AND id=p_credit FOR UPDATE;
  IF NOT FOUND OR credit_doc.status<>'issued'
     OR credit_doc.kind<>'customer_credit'
     OR credit_doc.source_invoice_id IS DISTINCT FROM p_original
     OR credit_doc.partner_id<>original_doc.partner_id
     OR credit_doc.currency<>original_doc.currency THEN
    RAISE EXCEPTION 'Incompatible issued customer credit';
  END IF;
  IF (SELECT count(*) FROM one_invoice_lines
       WHERE tenant_id=p_tenant AND doc_id=p_credit)<>1 THEN
    RAISE EXCEPTION 'Only single-item return credits supported in pilot';
  END IF;
  SELECT * INTO credit_line FROM one_invoice_lines
    WHERE tenant_id=p_tenant AND doc_id=p_credit AND line_number=1;
  IF NOT FOUND OR credit_line.quantity<>round(credit_line.quantity,3)
     OR credit_line.discount_amount<>0 THEN
    RAISE EXCEPTION 'Credit quantity precision and discount must match policy';
  END IF;
  SELECT invoice_line.quantity,il.*
    INTO matched_qty,original_line
    FROM (SELECT quantity FROM one_sales_invoice_allocations
          WHERE tenant_id=p_tenant AND invoice_id=p_original
            AND order_id=movement.order_id AND order_line_no=movement.line_no
         ) invoice_line
    JOIN one_sales_invoice_allocations alloc
      ON alloc.tenant_id=p_tenant AND alloc.invoice_id=p_original
      AND alloc.order_id=movement.order_id AND
         alloc.order_line_no=movement.line_no
    JOIN one_invoice_lines il ON il.tenant_id=alloc.tenant_id
      AND il.doc_id=alloc.invoice_id AND
         il.line_number=alloc.invoice_line_number;
  IF NOT FOUND OR original_line.discount_amount<>0
     OR credit_line.unit_price<>original_line.unit_price
     OR credit_line.account_id<>original_line.account_id
     OR credit_line.tax_category<>original_line.tax_category
     OR credit_line.tax_rate<>original_line.tax_rate
     OR credit_line.tax_code<>original_line.tax_code
     OR credit_line.input_tax_recoverable<>original_line.input_tax_recoverable THEN
    RAISE EXCEPTION 'Credit must match source invoice item, price and tax';
  END IF;
  SELECT coalesce(sum(quantity),0) INTO claimed_by_job
    FROM one_customer_return_credits
    WHERE tenant_id=p_tenant AND return_job_id=p_return;
  IF claimed_by_job+credit_line.quantity>j.accepted_qty THEN
    RAISE EXCEPTION 'Credit quantity exceeds accepted customer return';
  END IF;
  SELECT coalesce(sum(c.quantity),0) INTO claimed_from_original
    FROM one_customer_return_credits c
    JOIN one_return_jobs r ON r.tenant_id=c.tenant_id
       AND r.id=c.return_job_id
    JOIN one_stock_events e ON e.tenant_id=r.tenant_id
       AND e.id=r.source_event_id
    WHERE c.tenant_id=p_tenant AND c.original_invoice_id=p_original
      AND e.order_id=movement.order_id AND e.line_no=movement.line_no;
  IF claimed_from_original+credit_line.quantity>matched_qty THEN
    RAISE EXCEPTION 'Credit quantity exceeds source billed quantity';
  END IF;
  INSERT INTO one_customer_return_credits
    (tenant_id,credit_invoice_id,original_invoice_id,return_job_id,quantity,actor_id)
    VALUES(p_tenant,p_credit,p_original,p_return,credit_line.quantity,p_actor);
  RETURN p_credit;
END $$;
REVOKE ALL ON one_customer_return_credits FROM PUBLIC;
REVOKE ALL ON FUNCTION one_link_customer_return_credit(
  uuid,uuid,uuid,uuid,uuid) FROM PUBLIC;
COMMIT;
