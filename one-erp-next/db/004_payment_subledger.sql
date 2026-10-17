-- ONE ERP Next: internal payment allocation subledger / PostgreSQL 14+
-- Apply after 001, 002 and 003. Does NOT connect to a bank.
BEGIN;
CREATE TABLE one_payment_docs (
    tenant_id uuid NOT NULL REFERENCES one_tenants(id),
    id uuid NOT NULL,
    partner_id text NOT NULL CHECK (length(btrim(partner_id))>0),
    occurred_on date NOT NULL,
    currency text NOT NULL REFERENCES one_currency_precision(code),
    direction text NOT NULL CHECK (direction IN
        ('customer_receipt','vendor_disbursement')),
    amount numeric(20,3) NOT NULL CHECK (amount>0),
    bank_reference text NOT NULL CHECK (length(btrim(bank_reference))>0),
    bank_account_id text NOT NULL,
    control_account_id text NOT NULL,
    journal_id uuid NOT NULL,
    status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','applied')),
    applied_at timestamptz,
    applied_by uuid,
    PRIMARY KEY (tenant_id,id),
    UNIQUE (tenant_id,journal_id),
    FOREIGN KEY (tenant_id,journal_id)
        REFERENCES one_journal_entries(tenant_id,id),
    FOREIGN KEY (tenant_id,bank_account_id)
        REFERENCES one_accounts(tenant_id,id),
    FOREIGN KEY (tenant_id,control_account_id)
        REFERENCES one_accounts(tenant_id,id),
    CHECK ((status='draft' AND applied_at IS NULL AND applied_by IS NULL)
        OR (status='applied' AND applied_at IS NOT NULL AND applied_by IS NOT NULL))
);
CREATE TABLE one_payment_allocations (
    tenant_id uuid NOT NULL,
    payment_id uuid NOT NULL,
    invoice_id uuid NOT NULL,
    amount numeric(20,3) NOT NULL CHECK (amount>0),
    PRIMARY KEY (tenant_id,payment_id,invoice_id),
    FOREIGN KEY (tenant_id,payment_id)
        REFERENCES one_payment_docs(tenant_id,id),
    FOREIGN KEY (tenant_id,invoice_id)
        REFERENCES one_invoice_docs(tenant_id,id)
);
CREATE INDEX one_allocation_invoice_lookup
    ON one_payment_allocations(tenant_id,invoice_id);
CREATE TABLE one_payment_audit (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id uuid NOT NULL,
    payment_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    event text NOT NULL DEFAULT 'payment.applied' CHECK (event='payment.applied'),
    occurred_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    FOREIGN KEY (tenant_id,payment_id)
        REFERENCES one_payment_docs(tenant_id,id),
    UNIQUE (tenant_id,payment_id)
);

ALTER TABLE one_payment_docs ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_payment_docs FORCE ROW LEVEL SECURITY;
CREATE POLICY payment_doc_scope ON one_payment_docs
    USING (tenant_id::text=current_setting('one.tenant_id',true))
    WITH CHECK (tenant_id::text=current_setting('one.tenant_id',true));
ALTER TABLE one_payment_allocations ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_payment_allocations FORCE ROW LEVEL SECURITY;
CREATE POLICY payment_allocation_scope ON one_payment_allocations
    USING (tenant_id::text=current_setting('one.tenant_id',true))
    WITH CHECK (tenant_id::text=current_setting('one.tenant_id',true));
ALTER TABLE one_payment_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_payment_audit FORCE ROW LEVEL SECURITY;
CREATE POLICY payment_audit_scope ON one_payment_audit
    USING (tenant_id::text=current_setting('one.tenant_id',true))
    WITH CHECK (tenant_id::text=current_setting('one.tenant_id',true));

CREATE FUNCTION one_payment_doc_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='INSERT' THEN
        IF NEW.status<>'draft' OR NEW.applied_at IS NOT NULL OR
           NEW.applied_by IS NOT NULL THEN
            RAISE EXCEPTION 'Payment must start as draft';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP='DELETE' OR OLD.status='applied' THEN
        RAISE EXCEPTION 'Applied payment is immutable';
    END IF;
    IF NEW.tenant_id<>OLD.tenant_id OR NEW.id<>OLD.id THEN
        RAISE EXCEPTION 'Payment identity cannot change';
    END IF;
    IF NEW.status='applied' THEN
        IF current_setting('one.payment_context',true) IS DISTINCT FROM 'yes'
           OR ROW(NEW.partner_id,NEW.occurred_on,NEW.currency,NEW.direction,
             NEW.amount,NEW.bank_reference,NEW.bank_account_id,
             NEW.control_account_id,NEW.journal_id)
           IS DISTINCT FROM ROW(OLD.partner_id,OLD.occurred_on,OLD.currency,
             OLD.direction,OLD.amount,OLD.bank_reference,OLD.bank_account_id,
             OLD.control_account_id,OLD.journal_id) THEN
            RAISE EXCEPTION 'Only one_apply_payment may approve the unchanged draft';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER payment_doc_guard BEFORE INSERT OR UPDATE OR DELETE
    ON one_payment_docs FOR EACH ROW EXECUTE FUNCTION one_payment_doc_guard();

CREATE FUNCTION one_payment_allocation_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE state text;
BEGIN
    IF TG_OP='UPDATE' AND (
        NEW.tenant_id<>OLD.tenant_id OR
        NEW.payment_id<>OLD.payment_id OR NEW.invoice_id<>OLD.invoice_id
    ) THEN
        RAISE EXCEPTION 'Allocation identity immutable';
    END IF;
    SELECT status INTO state FROM one_payment_docs
      WHERE tenant_id=CASE WHEN TG_OP='DELETE' THEN OLD.tenant_id ELSE NEW.tenant_id END
        AND id=CASE WHEN TG_OP='DELETE' THEN OLD.payment_id ELSE NEW.payment_id END
      FOR UPDATE;
    IF state IS DISTINCT FROM 'draft' THEN
        RAISE EXCEPTION 'Applied payment allocations cannot change';
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER payment_allocation_guard BEFORE INSERT OR UPDATE OR DELETE
    ON one_payment_allocations FOR EACH ROW
    EXECUTE FUNCTION one_payment_allocation_guard();
CREATE TRIGGER payment_audit_immutable BEFORE UPDATE OR DELETE
    ON one_payment_audit FOR EACH ROW EXECUTE FUNCTION one_protect_audit();

CREATE FUNCTION one_apply_payment(p_tenant uuid,p_payment uuid,p_actor uuid)
RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE
    p one_payment_docs%ROWTYPE;
    j one_journal_entries%ROWTYPE;
    inv one_invoice_docs%ROWTYPE;
    item record;
    allocations_count integer;
    allocated numeric(24,3);
    prior_paid numeric(24,3);
    issued_credits numeric(24,3);
    unit_scale smallint;
    expected_kind text;
BEGIN
    IF current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text
       OR current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text
       OR NOT EXISTS (
          SELECT 1 FROM one_memberships
          WHERE tenant_id=p_tenant AND actor_id=p_actor AND active
            AND role IN ('accountant','company_admin')
       ) THEN
        RAISE EXCEPTION 'Authorized accountant required';
    END IF;
    SELECT * INTO p FROM one_payment_docs
      WHERE tenant_id=p_tenant AND id=p_payment FOR UPDATE;
    IF NOT FOUND OR p.status<>'draft' THEN
        RAISE EXCEPTION 'Payment missing or already applied';
    END IF;
    SELECT * INTO j FROM one_journal_entries
      WHERE tenant_id=p_tenant AND id=p.journal_id FOR UPDATE;
    IF NOT FOUND OR j.status<>'posted' OR j.currency<>p.currency
       OR j.occurred_on<>p.occurred_on
       OR j.reference<>p.direction||':'||p.bank_reference
       OR j.idempotency_key<>'payment:'||p.id::text THEN
        RAISE EXCEPTION 'Linked posted payment journal does not match request';
    END IF;
    SELECT minor_units INTO unit_scale FROM one_currency_precision
      WHERE code=p.currency;
    IF p.amount<>round(p.amount,unit_scale) OR
       (SELECT coalesce(sum(debit),0) FROM one_journal_lines
        WHERE tenant_id=p_tenant AND entry_id=p.journal_id)<>p.amount THEN
        RAISE EXCEPTION 'Payment journal total/precision mismatch';
    END IF;
    expected_kind := CASE WHEN p.direction='customer_receipt'
                          THEN 'asset' ELSE 'liability' END;
    IF NOT EXISTS (SELECT 1 FROM one_accounts
      WHERE tenant_id=p_tenant AND id=p.bank_account_id AND kind='asset'
        AND active AND posting_allowed)
      OR NOT EXISTS (SELECT 1 FROM one_accounts
      WHERE tenant_id=p_tenant AND id=p.control_account_id
        AND kind=expected_kind AND active AND posting_allowed) THEN
        RAISE EXCEPTION 'Invalid payment control accounts';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM one_journal_lines
        WHERE tenant_id=p_tenant AND entry_id=p.journal_id
          AND account_id=p.bank_account_id
          AND CASE WHEN p.direction='customer_receipt'
                   THEN debit=p.amount AND credit=0
                   ELSE credit=p.amount AND debit=0 END
    ) OR NOT EXISTS (
        SELECT 1 FROM one_journal_lines
        WHERE tenant_id=p_tenant AND entry_id=p.journal_id
          AND account_id=p.control_account_id
          AND CASE WHEN p.direction='customer_receipt'
                   THEN credit=p.amount AND debit=0
                   ELSE debit=p.amount AND credit=0 END
    ) THEN
        RAISE EXCEPTION 'Bank/control sides of GL payment do not match';
    END IF;

    SELECT count(*),coalesce(sum(amount),0) INTO allocations_count,allocated
      FROM one_payment_allocations
      WHERE tenant_id=p_tenant AND payment_id=p_payment;
    IF allocations_count<1 OR allocated<>p.amount
       OR EXISTS (
        SELECT 1 FROM one_payment_allocations
        WHERE tenant_id=p_tenant AND payment_id=p_payment
          AND amount<>round(amount,unit_scale)
       ) THEN
        RAISE EXCEPTION 'Incomplete payment allocation or invalid precision';
    END IF;
    FOR item IN SELECT invoice_id,amount FROM one_payment_allocations
      WHERE tenant_id=p_tenant AND payment_id=p_payment
      ORDER BY invoice_id
    LOOP
        -- Serialize all payments and credit issuance against the same invoice.
        SELECT * INTO inv FROM one_invoice_docs
          WHERE tenant_id=p_tenant AND id=item.invoice_id FOR UPDATE;
        IF NOT FOUND OR inv.status<>'issued' OR inv.partner_id<>p.partner_id
          OR inv.currency<>p.currency OR inv.issued_on>p.occurred_on
          OR (p.direction='customer_receipt' AND inv.kind<>'customer_invoice')
          OR (p.direction='vendor_disbursement' AND inv.kind<>'vendor_bill')
          OR inv.control_account_id<>p.control_account_id THEN
            RAISE EXCEPTION 'Incompatible/open original invoice required';
        END IF;
        SELECT coalesce(sum(a.amount),0) INTO prior_paid
          FROM one_payment_allocations a
          JOIN one_payment_docs d ON d.tenant_id=a.tenant_id
            AND d.id=a.payment_id
          WHERE a.tenant_id=p_tenant AND a.invoice_id=inv.id
            AND d.status='applied';
        SELECT coalesce(sum(gross_total),0) INTO issued_credits
          FROM one_invoice_docs WHERE tenant_id=p_tenant
          AND source_invoice_id=inv.id AND status='issued';
        IF item.amount+prior_paid+issued_credits>inv.gross_total THEN
            RAISE EXCEPTION 'Payment exceeds outstanding invoice balance';
        END IF;
    END LOOP;
    PERFORM set_config('one.payment_context','yes',true);
    UPDATE one_payment_docs SET status='applied',
      applied_at=transaction_timestamp(),applied_by=p_actor
      WHERE tenant_id=p_tenant AND id=p_payment;
    PERFORM set_config('one.payment_context','',true);
    INSERT INTO one_payment_audit(tenant_id,payment_id,actor_id)
      VALUES (p_tenant,p_payment,p_actor);
    RETURN p_payment;
END $$;
REVOKE ALL ON FUNCTION one_apply_payment(uuid,uuid,uuid) FROM PUBLIC;

-- A later credit note cannot refund an already settled amount.
CREATE FUNCTION one_guard_credit_against_payments()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE paid numeric(24,3); credited numeric(24,3); max_total numeric(24,3);
BEGIN
    IF NEW.status='issued' AND NEW.source_invoice_id IS NOT NULL
       AND OLD.status='draft' THEN
        -- one_issue_invoice already locks source document.
        SELECT gross_total INTO max_total FROM one_invoice_docs
           WHERE tenant_id=NEW.tenant_id AND id=NEW.source_invoice_id;
        SELECT coalesce(sum(a.amount),0) INTO paid
        FROM one_payment_allocations a
        JOIN one_payment_docs p ON p.tenant_id=a.tenant_id
              AND p.id=a.payment_id
        WHERE a.tenant_id=NEW.tenant_id
          AND a.invoice_id=NEW.source_invoice_id AND p.status='applied';
        SELECT coalesce(sum(gross_total),0) INTO credited
          FROM one_invoice_docs WHERE tenant_id=NEW.tenant_id
            AND source_invoice_id=NEW.source_invoice_id AND status='issued';
        IF paid+credited>max_total THEN
            RAISE EXCEPTION 'Credit note exceeds balance after existing payments';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER credit_against_payments AFTER UPDATE ON one_invoice_docs
  FOR EACH ROW EXECUTE FUNCTION one_guard_credit_against_payments();
COMMIT;
