-- ONE ERP Next migration 002: period close and exact reversal lineage.
-- Run AFTER 001_ledger.sql on a disposable DB first.
BEGIN;

ALTER TABLE one_journal_entries
    ADD COLUMN reversal_of uuid NULL,
    ADD CONSTRAINT one_reversal_source_fk
      FOREIGN KEY (tenant_id,reversal_of)
      REFERENCES one_journal_entries(tenant_id,id),
    ADD CONSTRAINT one_no_self_reversal
      CHECK (reversal_of IS NULL OR reversal_of <> id);

CREATE UNIQUE INDEX one_one_reversal_per_source
    ON one_journal_entries (tenant_id,reversal_of)
    WHERE reversal_of IS NOT NULL;

CREATE FUNCTION one_guard_reversal() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    original one_journal_entries%ROWTYPE;
    originals_count integer;
    reversals_count integer;
BEGIN
    IF OLD.status = 'posted' OR NEW.status <> 'posted'
       OR NEW.reversal_of IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT * INTO original FROM one_journal_entries
     WHERE tenant_id = NEW.tenant_id AND id = NEW.reversal_of FOR UPDATE;
    IF NOT FOUND OR original.status <> 'posted'
       OR original.reversal_of IS NOT NULL
       OR original.currency <> NEW.currency
       OR NEW.occurred_on < original.occurred_on THEN
        RAISE EXCEPTION 'Invalid reversal source or date/currency';
    END IF;
    SELECT count(*) INTO originals_count FROM one_journal_lines
      WHERE tenant_id=NEW.tenant_id AND entry_id=original.id;
    SELECT count(*) INTO reversals_count FROM one_journal_lines
      WHERE tenant_id=NEW.tenant_id AND entry_id=NEW.id;
    IF originals_count <> reversals_count OR EXISTS (
       SELECT 1 FROM one_journal_lines o
        WHERE o.tenant_id=NEW.tenant_id AND o.entry_id=original.id
        AND NOT EXISTS (
            SELECT 1 FROM one_journal_lines r
              WHERE r.tenant_id=NEW.tenant_id AND r.entry_id=NEW.id
                AND r.line_number=o.line_number
                AND r.account_id=o.account_id
                AND r.debit=o.credit AND r.credit=o.debit
        )
    ) THEN
        RAISE EXCEPTION 'Reversal must exactly mirror and invert original lines';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER reversal_guard BEFORE UPDATE ON one_journal_entries
    FOR EACH ROW EXECUTE FUNCTION one_guard_reversal();

ALTER TABLE one_periods
    ADD COLUMN closed_at timestamptz,
    ADD COLUMN closed_by uuid,
    ADD CONSTRAINT one_period_close_integrity CHECK (
        (closed=false AND closed_at IS NULL AND closed_by IS NULL) OR
        (closed=true AND closed_at IS NOT NULL AND closed_by IS NOT NULL)
    );

CREATE TABLE one_period_close_audit (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id uuid NOT NULL,
    period_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
    event text NOT NULL DEFAULT 'period.closed' CHECK (event='period.closed'),
    FOREIGN KEY (tenant_id,period_id) REFERENCES one_periods(tenant_id,id),
    UNIQUE (tenant_id,period_id)
);
ALTER TABLE one_period_close_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_period_close_audit FORCE ROW LEVEL SECURITY;
CREATE POLICY period_close_audit_scope ON one_period_close_audit
    USING (tenant_id::text = current_setting('one.tenant_id',true))
    WITH CHECK (tenant_id::text = current_setting('one.tenant_id',true));
CREATE TRIGGER period_close_audit_immutable BEFORE UPDATE OR DELETE
    ON one_period_close_audit
    FOR EACH ROW EXECUTE FUNCTION one_protect_audit();

CREATE FUNCTION one_guard_period() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Accounting periods must not be deleted';
    END IF;
    IF OLD.closed OR NEW.tenant_id <> OLD.tenant_id
       OR NEW.id <> OLD.id OR NEW.begins <> OLD.begins
       OR NEW.ends_exclusive <> OLD.ends_exclusive THEN
        RAISE EXCEPTION 'Period identity, bounds and closed state are immutable';
    END IF;
    IF NEW.closed IS DISTINCT FROM TRUE
       OR current_setting('one.closing_context',true) IS DISTINCT FROM 'yes'
       OR NEW.closed_at IS NULL OR NEW.closed_by IS NULL THEN
        RAISE EXCEPTION 'Use one_close_period to close an accounting period';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER period_guard BEFORE UPDATE OR DELETE ON one_periods
    FOR EACH ROW EXECUTE FUNCTION one_guard_period();

CREATE FUNCTION one_close_period(p_tenant uuid, p_period uuid, p_actor uuid)
RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE p one_periods%ROWTYPE;
BEGIN
    IF current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text
      OR current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text
      OR NOT EXISTS (
          SELECT 1 FROM one_memberships WHERE tenant_id=p_tenant
            AND actor_id=p_actor AND active AND role='company_admin'
      ) THEN
        RAISE EXCEPTION 'Authorized company admin required for period closing';
    END IF;
    SELECT * INTO p FROM one_periods
        WHERE tenant_id=p_tenant AND id=p_period FOR UPDATE;
    IF NOT FOUND OR p.closed THEN
        RAISE EXCEPTION 'Period does not exist or is already closed';
    END IF;
    PERFORM set_config('one.closing_context','yes',true);
    UPDATE one_periods SET closed=true,
        closed_at=transaction_timestamp(), closed_by=p_actor
      WHERE tenant_id=p_tenant AND id=p_period;
    PERFORM set_config('one.closing_context','',true);
    INSERT INTO one_period_close_audit(tenant_id,period_id,actor_id)
      VALUES (p_tenant,p_period,p_actor);
    RETURN p_period;
END $$;
REVOKE ALL ON FUNCTION one_close_period(uuid,uuid,uuid) FROM PUBLIC;
COMMIT;
