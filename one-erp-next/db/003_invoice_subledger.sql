-- ONE ERP Next: invoice subledger and immutable issue snapshot (PG14+).
-- Apply after 001 and 002. No official e-invoice issuance occurs here.
BEGIN;

CREATE TABLE one_invoice_docs (
    tenant_id uuid NOT NULL REFERENCES one_tenants(id),
    id uuid NOT NULL,
    kind text NOT NULL CHECK (kind IN
        ('customer_invoice','vendor_bill','customer_credit','vendor_credit')),
    number text NOT NULL CHECK (length(btrim(number))>0),
    partner_id text NOT NULL CHECK (length(btrim(partner_id))>0),
    issued_on date NOT NULL,
    due_on date NOT NULL,
    currency text NOT NULL REFERENCES one_currency_precision(code),
    source_invoice_id uuid,
    control_account_id text NOT NULL,
    journal_id uuid NOT NULL,
    net_total numeric(20,3) NOT NULL CHECK (net_total>0),
    tax_total numeric(20,3) NOT NULL CHECK (tax_total>=0),
    gross_total numeric(20,3) NOT NULL CHECK (gross_total>0),
    status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','issued')),
    approved_at timestamptz,
    approved_by uuid,
    PRIMARY KEY (tenant_id,id),
    UNIQUE (tenant_id,kind,number),
    UNIQUE (tenant_id,journal_id),
    FOREIGN KEY (tenant_id,journal_id)
        REFERENCES one_journal_entries(tenant_id,id),
    FOREIGN KEY (tenant_id,control_account_id)
        REFERENCES one_accounts(tenant_id,id),
    FOREIGN KEY (tenant_id,source_invoice_id)
        REFERENCES one_invoice_docs(tenant_id,id),
    CHECK (due_on>=issued_on),
    CHECK (gross_total=net_total+tax_total),
    CHECK (
        (kind IN ('customer_credit','vendor_credit') AND source_invoice_id IS NOT NULL)
        OR (kind IN ('customer_invoice','vendor_bill') AND source_invoice_id IS NULL)
    ),
    CHECK ((status='draft' AND approved_at IS NULL AND approved_by IS NULL)
        OR (status='issued' AND approved_at IS NOT NULL AND approved_by IS NOT NULL))
);

CREATE TABLE one_invoice_lines (
    tenant_id uuid NOT NULL,
    doc_id uuid NOT NULL,
    line_number integer NOT NULL CHECK (line_number>0),
    description text NOT NULL CHECK (length(btrim(description))>0),
    account_id text NOT NULL,
    quantity numeric(20,6) NOT NULL CHECK (quantity>0),
    unit_price numeric(20,3) NOT NULL CHECK (unit_price>=0),
    discount_amount numeric(20,3) NOT NULL CHECK (discount_amount>=0),
    tax_category text NOT NULL CHECK (tax_category IN
        ('standard','zero_rated','exempt','out_of_scope')),
    tax_code text NOT NULL,
    tax_rate numeric(8,6) NOT NULL CHECK (tax_rate>=0 AND tax_rate<=1),
    input_tax_recoverable boolean NOT NULL DEFAULT false,
    base_total numeric(20,3) NOT NULL CHECK (base_total>=0),
    tax_total numeric(20,3) NOT NULL CHECK (tax_total>=0),
    PRIMARY KEY (tenant_id,doc_id,line_number),
    FOREIGN KEY (tenant_id,doc_id) REFERENCES one_invoice_docs(tenant_id,id),
    FOREIGN KEY (tenant_id,account_id) REFERENCES one_accounts(tenant_id,id),
    CHECK (
      (tax_category='standard' AND tax_rate>0 AND length(btrim(tax_code))>0)
      OR (tax_category<>'standard' AND tax_rate=0 AND tax_total=0)
    )
);

CREATE TABLE one_invoice_audit (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id uuid NOT NULL,
    doc_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    event text NOT NULL DEFAULT 'invoice.issued' CHECK (event='invoice.issued'),
    occurred_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    FOREIGN KEY (tenant_id,doc_id) REFERENCES one_invoice_docs(tenant_id,id),
    UNIQUE (tenant_id,doc_id)
);

ALTER TABLE one_invoice_docs ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_invoice_docs FORCE ROW LEVEL SECURITY;
CREATE POLICY invoice_doc_scope ON one_invoice_docs
    USING (tenant_id::text=current_setting('one.tenant_id',true))
    WITH CHECK (tenant_id::text=current_setting('one.tenant_id',true));
ALTER TABLE one_invoice_lines ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_invoice_lines FORCE ROW LEVEL SECURITY;
CREATE POLICY invoice_line_scope ON one_invoice_lines
    USING (tenant_id::text=current_setting('one.tenant_id',true))
    WITH CHECK (tenant_id::text=current_setting('one.tenant_id',true));
ALTER TABLE one_invoice_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_invoice_audit FORCE ROW LEVEL SECURITY;
CREATE POLICY invoice_audit_scope ON one_invoice_audit
    USING (tenant_id::text=current_setting('one.tenant_id',true))
    WITH CHECK (tenant_id::text=current_setting('one.tenant_id',true));

CREATE FUNCTION one_invoice_line_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE state text;
BEGIN
    IF TG_OP='UPDATE' AND (
        NEW.tenant_id<>OLD.tenant_id OR NEW.doc_id<>OLD.doc_id
        OR NEW.line_number<>OLD.line_number
    ) THEN
        RAISE EXCEPTION 'Invoice line identity cannot change';
    END IF;
    SELECT status INTO state FROM one_invoice_docs
        WHERE tenant_id=CASE WHEN TG_OP='DELETE' THEN OLD.tenant_id ELSE NEW.tenant_id END
          AND id=CASE WHEN TG_OP='DELETE' THEN OLD.doc_id ELSE NEW.doc_id END
        FOR UPDATE;
    IF state IS DISTINCT FROM 'draft' THEN
        RAISE EXCEPTION 'Invoice issued or not found; lines immutable';
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER invoice_line_guard BEFORE INSERT OR UPDATE OR DELETE
    ON one_invoice_lines FOR EACH ROW EXECUTE FUNCTION one_invoice_line_guard();

CREATE FUNCTION one_invoice_doc_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' OR OLD.status='issued' THEN
        RAISE EXCEPTION 'Issued invoices cannot be changed or removed';
    END IF;
    IF NEW.tenant_id<>OLD.tenant_id OR NEW.id<>OLD.id THEN
        RAISE EXCEPTION 'Invoice identity immutable';
    END IF;
    IF NEW.status='issued' THEN
        IF current_setting('one.invoice_context',true) IS DISTINCT FROM 'yes' OR
           ROW(NEW.kind,NEW.number,NEW.partner_id,NEW.issued_on,NEW.due_on,
               NEW.currency,NEW.source_invoice_id,NEW.control_account_id,
               NEW.journal_id,NEW.net_total,NEW.tax_total,NEW.gross_total)
           IS DISTINCT FROM
           ROW(OLD.kind,OLD.number,OLD.partner_id,OLD.issued_on,OLD.due_on,
               OLD.currency,OLD.source_invoice_id,OLD.control_account_id,
               OLD.journal_id,OLD.net_total,OLD.tax_total,OLD.gross_total)
        THEN
            RAISE EXCEPTION 'Only one_issue_invoice may issue an unchanged draft';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER invoice_doc_guard BEFORE UPDATE OR DELETE
    ON one_invoice_docs FOR EACH ROW EXECUTE FUNCTION one_invoice_doc_guard();
CREATE TRIGGER invoice_audit_immutable BEFORE UPDATE OR DELETE
    ON one_invoice_audit FOR EACH ROW EXECUTE FUNCTION one_protect_audit();

CREATE FUNCTION one_issue_invoice(p_tenant uuid,p_doc uuid,p_actor uuid)
RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE
    d one_invoice_docs%ROWTYPE;
    j one_journal_entries%ROWTYPE;
    source_doc one_invoice_docs%ROWTYPE;
    n integer;
    bases numeric(24,3);
    taxes numeric(24,3);
    scale smallint;
    prior_credited numeric(24,3);
    control_count integer;
    expected_kind text;
BEGIN
    IF current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text
       OR current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text
       OR NOT EXISTS (
         SELECT 1 FROM one_memberships
         WHERE tenant_id=p_tenant AND actor_id=p_actor AND active
           AND role IN ('accountant','company_admin')
       ) THEN
        RAISE EXCEPTION 'Authorized accounting user required';
    END IF;
    SELECT * INTO d FROM one_invoice_docs
      WHERE tenant_id=p_tenant AND id=p_doc FOR UPDATE;
    IF NOT FOUND OR d.status<>'draft' THEN
        RAISE EXCEPTION 'Invoice missing or already issued';
    END IF;
    SELECT * INTO j FROM one_journal_entries
      WHERE tenant_id=p_tenant AND id=d.journal_id FOR UPDATE;
    IF NOT FOUND OR j.status<>'posted' OR j.currency<>d.currency OR
       j.occurred_on<>d.issued_on OR
       j.reference<>d.kind||':'||d.number OR
       j.idempotency_key<>'invoice:'||d.id::text THEN
        RAISE EXCEPTION 'Linked posted journal must match invoice identity';
    END IF;
    SELECT minor_units INTO scale FROM one_currency_precision WHERE code=d.currency;
    SELECT count(*),coalesce(sum(base_total),0),coalesce(sum(tax_total),0)
      INTO n,bases,taxes FROM one_invoice_lines
      WHERE tenant_id=p_tenant AND doc_id=d.id;
    IF n<1 OR bases<>d.net_total OR taxes<>d.tax_total OR
       EXISTS (SELECT 1 FROM one_invoice_lines l
               WHERE l.tenant_id=p_tenant AND l.doc_id=d.id AND (
                   l.base_total<>round(l.quantity*l.unit_price-l.discount_amount,scale)
                   OR l.tax_total<>round(l.base_total*l.tax_rate,scale)
                   OR l.unit_price<>round(l.unit_price,scale)
                   OR l.discount_amount<>round(l.discount_amount,scale)
                   OR (l.tax_category='standard' AND l.tax_code='NONE')
                   OR (d.kind IN ('customer_invoice','customer_credit')
                       AND l.input_tax_recoverable)
               )) OR
       EXISTS (SELECT 1 FROM one_invoice_lines l JOIN one_accounts a
               ON a.tenant_id=l.tenant_id AND a.id=l.account_id
               WHERE l.tenant_id=p_tenant AND l.doc_id=d.id AND
                  (NOT a.active OR NOT a.posting_allowed OR
                   a.kind<>CASE WHEN d.kind IN
                     ('customer_invoice','customer_credit') THEN 'revenue'
                     ELSE 'expense' END)) OR
       EXISTS (SELECT 1 FROM one_journal_lines l WHERE l.tenant_id=p_tenant
               AND l.entry_id=d.journal_id AND
               (l.debit<>round(l.debit,scale) OR l.credit<>round(l.credit,scale)))
    THEN
        RAISE EXCEPTION 'Invoice line and tax snapshot mismatch';
    END IF;
    IF (SELECT coalesce(sum(debit),0) FROM one_journal_lines
         WHERE tenant_id=p_tenant AND entry_id=d.journal_id)<>d.gross_total THEN
        RAISE EXCEPTION 'Journal posting total differs from invoice';
    END IF;
    expected_kind := CASE WHEN d.kind IN ('customer_invoice','customer_credit')
                          THEN 'asset' ELSE 'liability' END;
    IF NOT EXISTS (
        SELECT 1 FROM one_accounts a WHERE a.tenant_id=p_tenant
          AND a.id=d.control_account_id AND a.kind=expected_kind
          AND a.active AND a.posting_allowed
    ) THEN
        RAISE EXCEPTION 'Invalid invoice control account';
    END IF;
    SELECT count(*) INTO control_count FROM one_journal_lines l
       WHERE l.tenant_id=p_tenant AND l.entry_id=d.journal_id
         AND l.account_id=d.control_account_id
         AND CASE WHEN d.kind IN ('customer_invoice','vendor_credit')
             THEN l.debit=d.gross_total AND l.credit=0
             ELSE l.credit=d.gross_total AND l.debit=0 END;
    IF control_count<>1 THEN
        RAISE EXCEPTION 'Invoice control balance absent or incorrect';
    END IF;
    IF d.source_invoice_id IS NOT NULL THEN
        -- Lock original to serialize partial-credit issuance across sessions.
        SELECT * INTO source_doc FROM one_invoice_docs
          WHERE tenant_id=p_tenant AND id=d.source_invoice_id FOR UPDATE;
        IF NOT FOUND OR source_doc.status<>'issued' OR
           source_doc.partner_id<>d.partner_id OR
           source_doc.currency<>d.currency OR
           source_doc.issued_on>d.issued_on OR
           (d.kind='customer_credit' AND source_doc.kind<>'customer_invoice') OR
           (d.kind='vendor_credit' AND source_doc.kind<>'vendor_bill') THEN
            RAISE EXCEPTION 'Credit note must reference issued compatible source';
        END IF;
        SELECT coalesce(sum(gross_total),0) INTO prior_credited
          FROM one_invoice_docs
          WHERE tenant_id=p_tenant AND source_invoice_id=d.source_invoice_id
            AND status='issued';
        IF prior_credited+d.gross_total>source_doc.gross_total THEN
            RAISE EXCEPTION 'Total issued credits exceed original invoice';
        END IF;
    END IF;
    PERFORM set_config('one.invoice_context','yes',true);
    UPDATE one_invoice_docs SET status='issued',
        approved_at=transaction_timestamp(),approved_by=p_actor
      WHERE tenant_id=p_tenant AND id=p_doc;
    PERFORM set_config('one.invoice_context','',true);
    INSERT INTO one_invoice_audit(tenant_id,doc_id,actor_id)
      VALUES (p_tenant,p_doc,p_actor);
    RETURN p_doc;
END $$;
REVOKE ALL ON FUNCTION one_issue_invoice(uuid,uuid,uuid) FROM PUBLIC;
COMMIT;
