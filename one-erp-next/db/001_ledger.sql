-- ONE ERP Next / PostgreSQL 14+
-- Foundation only. Apply on an isolated database using a dedicated migration role.
-- The application MUST authenticate the actor and use SET LOCAL on both GUCs.
-- App roles must not own tables or have BYPASSRLS/superuser privileges.
BEGIN;

CREATE TABLE one_tenants (
    id uuid PRIMARY KEY,
    company_name text NOT NULL CHECK (length(btrim(company_name)) > 0)
);
CREATE TABLE one_memberships (
    tenant_id uuid NOT NULL REFERENCES one_tenants(id),
    actor_id uuid NOT NULL,
    role text NOT NULL CHECK (role IN ('company_admin','accountant','viewer')),
    active boolean NOT NULL DEFAULT true,
    PRIMARY KEY (tenant_id, actor_id)
);
CREATE TABLE one_accounts (
    tenant_id uuid NOT NULL REFERENCES one_tenants(id),
    id text NOT NULL CHECK (length(btrim(id)) > 0),
    code text NOT NULL CHECK (length(btrim(code)) > 0),
    name text NOT NULL CHECK (length(btrim(name)) > 0),
    kind text NOT NULL CHECK (kind IN ('asset','liability','equity','revenue','expense')),
    active boolean NOT NULL DEFAULT true,
    posting_allowed boolean NOT NULL DEFAULT true,
    PRIMARY KEY (tenant_id,id),
    UNIQUE (tenant_id,code)
);
CREATE TABLE one_periods (
    tenant_id uuid NOT NULL REFERENCES one_tenants(id),
    id uuid NOT NULL,
    begins date NOT NULL,
    ends_exclusive date NOT NULL,
    closed boolean NOT NULL DEFAULT false,
    PRIMARY KEY (tenant_id,id),
    CHECK (begins < ends_exclusive)
);
-- Global ISO 4217 minor-unit policy; extend via reviewed migrations only.
CREATE TABLE one_currency_precision (
    code char(3) PRIMARY KEY,
    minor_units smallint NOT NULL CHECK (minor_units BETWEEN 0 AND 3)
);
INSERT INTO one_currency_precision(code,minor_units) VALUES
 ('SAR',2),('USD',2),('EGP',2),('AED',2),('EUR',2),
 ('INR',2),('SDG',2),('JPY',0),('KWD',3),('BHD',3),('OMR',3);

CREATE TABLE one_journal_entries (
    tenant_id uuid NOT NULL REFERENCES one_tenants(id),
    id uuid NOT NULL,
    period_id uuid NOT NULL,
    reference text NOT NULL CHECK (length(btrim(reference)) > 0),
    idempotency_key text NOT NULL CHECK (length(btrim(idempotency_key)) > 0),
    occurred_on date NOT NULL,
    currency text NOT NULL REFERENCES one_currency_precision(code) CHECK (currency ~ '^[A-Z]{3}
    status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','posted')),
    posted_at timestamptz,
    posted_by uuid,
    PRIMARY KEY (tenant_id,id),
    UNIQUE (tenant_id,idempotency_key),
    FOREIGN KEY (tenant_id,period_id) REFERENCES one_periods(tenant_id,id),
    CHECK ((status='draft' AND posted_at IS NULL AND posted_by IS NULL)
        OR (status='posted' AND posted_at IS NOT NULL AND posted_by IS NOT NULL))
);
CREATE TABLE one_journal_lines (
    tenant_id uuid NOT NULL,
    entry_id uuid NOT NULL,
    line_number integer NOT NULL CHECK (line_number > 0),
    account_id text NOT NULL,
    debit numeric(20,3) NOT NULL DEFAULT 0,
    credit numeric(20,3) NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id,entry_id,line_number),
    FOREIGN KEY (tenant_id,entry_id) REFERENCES one_journal_entries(tenant_id,id),
    FOREIGN KEY (tenant_id,account_id) REFERENCES one_accounts(tenant_id,id),
    CHECK ((debit > 0 AND credit=0) OR (credit > 0 AND debit=0))
);
CREATE TABLE one_ledger_audit (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id uuid NOT NULL,
    entry_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    event text NOT NULL CHECK (event='journal.posted'),
    occurred_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    FOREIGN KEY (tenant_id,entry_id) REFERENCES one_journal_entries(tenant_id,id)
);

-- RLS is mandatory on every tenant-owned table (including background reporting).
-- Missing SET LOCAL one.tenant_id denies access.
ALTER TABLE one_tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_tenants FORCE ROW LEVEL SECURITY;
CREATE POLICY tenants_scope ON one_tenants
    USING (id::text = current_setting('one.tenant_id',true))
    WITH CHECK (id::text = current_setting('one.tenant_id',true));
ALTER TABLE one_memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_memberships FORCE ROW LEVEL SECURITY;
CREATE POLICY memberships_scope ON one_memberships
    USING (tenant_id::text = current_setting('one.tenant_id',true))
    WITH CHECK (tenant_id::text = current_setting('one.tenant_id',true));
ALTER TABLE one_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_accounts FORCE ROW LEVEL SECURITY;
CREATE POLICY accounts_scope ON one_accounts
    USING (tenant_id::text = current_setting('one.tenant_id',true))
    WITH CHECK (tenant_id::text = current_setting('one.tenant_id',true));
ALTER TABLE one_periods ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_periods FORCE ROW LEVEL SECURITY;
CREATE POLICY periods_scope ON one_periods
    USING (tenant_id::text = current_setting('one.tenant_id',true))
    WITH CHECK (tenant_id::text = current_setting('one.tenant_id',true));
ALTER TABLE one_journal_entries ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_journal_entries FORCE ROW LEVEL SECURITY;
CREATE POLICY entries_scope ON one_journal_entries
    USING (tenant_id::text = current_setting('one.tenant_id',true))
    WITH CHECK (tenant_id::text = current_setting('one.tenant_id',true));
ALTER TABLE one_journal_lines ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_journal_lines FORCE ROW LEVEL SECURITY;
CREATE POLICY lines_scope ON one_journal_lines
    USING (tenant_id::text = current_setting('one.tenant_id',true))
    WITH CHECK (tenant_id::text = current_setting('one.tenant_id',true));
ALTER TABLE one_ledger_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_ledger_audit FORCE ROW LEVEL SECURITY;
CREATE POLICY audit_scope ON one_ledger_audit
    USING (tenant_id::text = current_setting('one.tenant_id',true))
    WITH CHECK (tenant_id::text = current_setting('one.tenant_id',true));

CREATE FUNCTION one_lock_lines() RETURNS trigger LANGUAGE plpgsql AS $
DECLARE s text;
BEGIN
    IF TG_OP = 'UPDATE' AND (
        NEW.tenant_id <> OLD.tenant_id OR NEW.entry_id <> OLD.entry_id
        OR NEW.line_number <> OLD.line_number
    ) THEN
        RAISE EXCEPTION 'Journal line identity cannot change';
    END IF;
    -- Serialize any line modification with posting of the parent journal.
    SELECT status INTO s FROM one_journal_entries
    WHERE tenant_id = CASE WHEN TG_OP='DELETE' THEN OLD.tenant_id ELSE NEW.tenant_id END
      AND id = CASE WHEN TG_OP='DELETE' THEN OLD.entry_id ELSE NEW.entry_id END
    FOR UPDATE;
    IF s IS NULL OR s <> 'draft' THEN
        RAISE EXCEPTION 'Only draft journals may have their lines changed';
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER lines_lock BEFORE INSERT OR UPDATE OR DELETE
    ON one_journal_lines FOR EACH ROW EXECUTE FUNCTION one_lock_lines();

CREATE FUNCTION one_protect_posted() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' OR OLD.status='posted' THEN
        RAISE EXCEPTION 'Posted journals are immutable; use a reversing entry';
    END IF;
    -- Never reassign a journal to a new tenant or ID during editing.
    IF NEW.tenant_id <> OLD.tenant_id OR NEW.id <> OLD.id THEN
        RAISE EXCEPTION 'Journal identity cannot change';
    END IF;
    IF NEW.status='posted' AND current_setting('one.posting_context',true) IS DISTINCT FROM 'yes' THEN
        RAISE EXCEPTION 'Use one_post_journal to post';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER entry_guard BEFORE UPDATE OR DELETE
    ON one_journal_entries FOR EACH ROW EXECUTE FUNCTION one_protect_posted();

CREATE FUNCTION one_protect_audit() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Audit history is append-only';
END $$;
CREATE TRIGGER audit_immutable BEFORE UPDATE OR DELETE
    ON one_ledger_audit FOR EACH ROW EXECUTE FUNCTION one_protect_audit();

-- SECURITY INVOKER means tenant RLS remains in force for the caller.
CREATE FUNCTION one_post_journal(p_tenant uuid, p_entry uuid, p_actor uuid)
RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE j one_journal_entries%ROWTYPE;
DECLARE p one_periods%ROWTYPE;
DECLARE n integer;
DECLARE d numeric(24,3);
DECLARE c numeric(24,3);
DECLARE scale smallint;
BEGIN
    IF current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text
       OR current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text THEN
        RAISE EXCEPTION 'Authenticated tenant/actor context required';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM one_memberships
        WHERE tenant_id=p_tenant AND actor_id=p_actor AND active
          AND role IN ('company_admin','accountant')
    ) THEN
        RAISE EXCEPTION 'Not authorized to post journals';
    END IF;
    SELECT * INTO j FROM one_journal_entries
    WHERE tenant_id=p_tenant AND id=p_entry FOR UPDATE;
    IF NOT FOUND OR j.status <> 'draft' THEN
        RAISE EXCEPTION 'Journal missing or already posted';
    END IF;
    SELECT * INTO p FROM one_periods
    WHERE tenant_id=p_tenant AND id=j.period_id FOR UPDATE;
    IF NOT FOUND OR p.closed OR j.occurred_on < p.begins
       OR j.occurred_on >= p.ends_exclusive THEN
        RAISE EXCEPTION 'Invalid or closed accounting period';
    END IF;
    SELECT count(*), coalesce(sum(l.debit),0), coalesce(sum(l.credit),0)
    INTO n,d,c FROM one_journal_lines l
    JOIN one_accounts a ON a.tenant_id=l.tenant_id AND a.id=l.account_id
    WHERE l.tenant_id=p_tenant AND l.entry_id=p_entry
      AND a.active AND a.posting_allowed;
    IF n < 2 OR d <> c OR d <= 0 OR n <> (
        SELECT count(*) FROM one_journal_lines
        WHERE tenant_id=p_tenant AND entry_id=p_entry
    ) THEN
        RAISE EXCEPTION 'Journal unbalanced, missing lines, or account disabled';
    END IF;
    SELECT minor_units INTO scale FROM one_currency_precision WHERE code=j.currency;
    IF scale IS NULL OR EXISTS (
        SELECT 1 FROM one_journal_lines
        WHERE tenant_id=p_tenant AND entry_id=p_entry
          AND (debit <> round(debit,scale) OR credit <> round(credit,scale))
    ) THEN
        RAISE EXCEPTION 'Journal amount exceeds currency minor units';
    END IF;
    PERFORM set_config('one.posting_context','yes',true);
    UPDATE one_journal_entries SET status='posted',
        posted_at=transaction_timestamp(), posted_by=p_actor
    WHERE tenant_id=p_tenant AND id=p_entry;
    PERFORM set_config('one.posting_context','',true);
    INSERT INTO one_ledger_audit(tenant_id,entry_id,actor_id,event)
    VALUES (p_tenant,p_entry,p_actor,'journal.posted');
    RETURN p_entry;
END $$;

-- Deny implicit public SQL execution. Explicitly grant only audited runtime roles.
REVOKE ALL ON FUNCTION one_post_journal(uuid,uuid,uuid) FROM PUBLIC;
-- PRODUCTION NOTE: restrict table privileges, function EXECUTE, GUC-setting
-- and tenant provisioning to a vetted server-side role. All DB credentials
-- belong in a secrets manager, never in repository files.
COMMIT;
),
    status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','posted')),
    posted_at timestamptz,
    posted_by uuid,
    PRIMARY KEY (tenant_id,id),
    UNIQUE (tenant_id,idempotency_key),
    FOREIGN KEY (tenant_id,period_id) REFERENCES one_periods(tenant_id,id),
    CHECK ((status='draft' AND posted_at IS NULL AND posted_by IS NULL)
        OR (status='posted' AND posted_at IS NOT NULL AND posted_by IS NOT NULL))
);
CREATE TABLE one_journal_lines (
    tenant_id uuid NOT NULL,
    entry_id uuid NOT NULL,
    line_number integer NOT NULL CHECK (line_number > 0),
    account_id text NOT NULL,
    debit numeric(20,3) NOT NULL DEFAULT 0,
    credit numeric(20,3) NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id,entry_id,line_number),
    FOREIGN KEY (tenant_id,entry_id) REFERENCES one_journal_entries(tenant_id,id),
    FOREIGN KEY (tenant_id,account_id) REFERENCES one_accounts(tenant_id,id),
    CHECK ((debit > 0 AND credit=0) OR (credit > 0 AND debit=0))
);
CREATE TABLE one_ledger_audit (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id uuid NOT NULL,
    entry_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    event text NOT NULL CHECK (event='journal.posted'),
    occurred_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    FOREIGN KEY (tenant_id,entry_id) REFERENCES one_journal_entries(tenant_id,id)
);

-- RLS is mandatory on every tenant-owned table (including background reporting).
-- Missing SET LOCAL one.tenant_id denies access.
ALTER TABLE one_tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_tenants FORCE ROW LEVEL SECURITY;
CREATE POLICY tenants_scope ON one_tenants
    USING (id::text = current_setting('one.tenant_id',true))
    WITH CHECK (id::text = current_setting('one.tenant_id',true));
ALTER TABLE one_memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_memberships FORCE ROW LEVEL SECURITY;
CREATE POLICY memberships_scope ON one_memberships
    USING (tenant_id::text = current_setting('one.tenant_id',true))
    WITH CHECK (tenant_id::text = current_setting('one.tenant_id',true));
ALTER TABLE one_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_accounts FORCE ROW LEVEL SECURITY;
CREATE POLICY accounts_scope ON one_accounts
    USING (tenant_id::text = current_setting('one.tenant_id',true))
    WITH CHECK (tenant_id::text = current_setting('one.tenant_id',true));
ALTER TABLE one_periods ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_periods FORCE ROW LEVEL SECURITY;
CREATE POLICY periods_scope ON one_periods
    USING (tenant_id::text = current_setting('one.tenant_id',true))
    WITH CHECK (tenant_id::text = current_setting('one.tenant_id',true));
ALTER TABLE one_journal_entries ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_journal_entries FORCE ROW LEVEL SECURITY;
CREATE POLICY entries_scope ON one_journal_entries
    USING (tenant_id::text = current_setting('one.tenant_id',true))
    WITH CHECK (tenant_id::text = current_setting('one.tenant_id',true));
ALTER TABLE one_journal_lines ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_journal_lines FORCE ROW LEVEL SECURITY;
CREATE POLICY lines_scope ON one_journal_lines
    USING (tenant_id::text = current_setting('one.tenant_id',true))
    WITH CHECK (tenant_id::text = current_setting('one.tenant_id',true));
ALTER TABLE one_ledger_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_ledger_audit FORCE ROW LEVEL SECURITY;
CREATE POLICY audit_scope ON one_ledger_audit
    USING (tenant_id::text = current_setting('one.tenant_id',true))
    WITH CHECK (tenant_id::text = current_setting('one.tenant_id',true));

CREATE FUNCTION one_lock_lines() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE s text;
BEGIN
    -- Serialize any line modification with posting of the parent journal.
    SELECT status INTO s FROM one_journal_entries
    WHERE tenant_id = CASE WHEN TG_OP='DELETE' THEN OLD.tenant_id ELSE NEW.tenant_id END
      AND id = CASE WHEN TG_OP='DELETE' THEN OLD.entry_id ELSE NEW.entry_id END
    FOR UPDATE;
    IF s IS NULL OR s <> 'draft' THEN
        RAISE EXCEPTION 'Only draft journals may have their lines changed';
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER lines_lock BEFORE INSERT OR UPDATE OR DELETE
    ON one_journal_lines FOR EACH ROW EXECUTE FUNCTION one_lock_lines();

CREATE FUNCTION one_protect_posted() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' OR OLD.status='posted' THEN
        RAISE EXCEPTION 'Posted journals are immutable; use a reversing entry';
    END IF;
    -- Never reassign a journal to a new tenant or ID during editing.
    IF NEW.tenant_id <> OLD.tenant_id OR NEW.id <> OLD.id THEN
        RAISE EXCEPTION 'Journal identity cannot change';
    END IF;
    IF NEW.status='posted' AND current_setting('one.posting_context',true) IS DISTINCT FROM 'yes' THEN
        RAISE EXCEPTION 'Use one_post_journal to post';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER entry_guard BEFORE UPDATE OR DELETE
    ON one_journal_entries FOR EACH ROW EXECUTE FUNCTION one_protect_posted();

CREATE FUNCTION one_protect_audit() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Audit history is append-only';
END $$;
CREATE TRIGGER audit_immutable BEFORE UPDATE OR DELETE
    ON one_ledger_audit FOR EACH ROW EXECUTE FUNCTION one_protect_audit();

-- SECURITY INVOKER means tenant RLS remains in force for the caller.
CREATE FUNCTION one_post_journal(p_tenant uuid, p_entry uuid, p_actor uuid)
RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE j one_journal_entries%ROWTYPE;
DECLARE p one_periods%ROWTYPE;
DECLARE n integer;
DECLARE d numeric(24,3);
DECLARE c numeric(24,3);
BEGIN
    IF current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text
       OR current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text THEN
        RAISE EXCEPTION 'Authenticated tenant/actor context required';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM one_memberships
        WHERE tenant_id=p_tenant AND actor_id=p_actor AND active
          AND role IN ('company_admin','accountant')
    ) THEN
        RAISE EXCEPTION 'Not authorized to post journals';
    END IF;
    SELECT * INTO j FROM one_journal_entries
    WHERE tenant_id=p_tenant AND id=p_entry FOR UPDATE;
    IF NOT FOUND OR j.status <> 'draft' THEN
        RAISE EXCEPTION 'Journal missing or already posted';
    END IF;
    SELECT * INTO p FROM one_periods
    WHERE tenant_id=p_tenant AND id=j.period_id FOR UPDATE;
    IF NOT FOUND OR p.closed OR j.occurred_on < p.begins
       OR j.occurred_on >= p.ends_exclusive THEN
        RAISE EXCEPTION 'Invalid or closed accounting period';
    END IF;
    SELECT count(*), coalesce(sum(l.debit),0), coalesce(sum(l.credit),0)
    INTO n,d,c FROM one_journal_lines l
    JOIN one_accounts a ON a.tenant_id=l.tenant_id AND a.id=l.account_id
    WHERE l.tenant_id=p_tenant AND l.entry_id=p_entry
      AND a.active AND a.posting_allowed;
    IF n < 2 OR d <> c OR d <= 0 OR n <> (
        SELECT count(*) FROM one_journal_lines
        WHERE tenant_id=p_tenant AND entry_id=p_entry
    ) THEN
        RAISE EXCEPTION 'Journal unbalanced, missing lines, or account disabled';
    END IF;
    PERFORM set_config('one.posting_context','yes',true);
    UPDATE one_journal_entries SET status='posted',
        posted_at=transaction_timestamp(), posted_by=p_actor
    WHERE tenant_id=p_tenant AND id=p_entry;
    PERFORM set_config('one.posting_context','',true);
    INSERT INTO one_ledger_audit(tenant_id,entry_id,actor_id,event)
    VALUES (p_tenant,p_entry,p_actor,'journal.posted');
    RETURN p_entry;
END $$;

-- PRODUCTION NOTE: restrict table privileges, function EXECUTE, GUC-setting
-- and tenant provisioning to a vetted server-side role. All DB credentials
-- belong in a secrets manager, never in repository files.
COMMIT;
