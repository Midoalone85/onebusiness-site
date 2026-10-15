-- ONE ERP Next / PostgreSQL 14+ / control-plane migration 003.
-- This is a JOURNALED PROVISIONING GATE, NOT an infrastructure provisioner.
-- Run in an isolated control-plane database after 001 and 002.
-- A trusted, separately authenticated admin orchestrator must verify each
-- real infrastructure step and pass its independently derived SHA-256 digest.
-- No browser, tenant database role or untrusted client may invoke functions.
BEGIN;

-- A new tenant is NEVER active before independent infrastructure readiness.
ALTER TABLE one_auth.tenant_registry ALTER COLUMN active SET DEFAULT false;

CREATE TABLE one_auth.tenant_bootstrap_jobs (
    job_id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL UNIQUE
        REFERENCES one_auth.tenant_registry(tenant_id),
    operator_id uuid NOT NULL,
    approval_reference text NOT NULL
        CHECK (approval_reference ~ '^[A-Za-z0-9._:-]{8,128}$'),
    state text NOT NULL DEFAULT 'requested'
        CHECK (state IN (
            'requested','database_created','schema_migrated',
            'isolation_verified','restore_verified',
            'route_verified','ready','failed'
        )),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    changed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- A separate append-only journal records each transition, excluding secrets,
-- credentials, connection strings, backup keys, personal data and tokens.
CREATE TABLE one_auth.tenant_bootstrap_events (
    event_id uuid PRIMARY KEY,
    job_id uuid NOT NULL REFERENCES one_auth.tenant_bootstrap_jobs(job_id),
    tenant_id uuid NOT NULL,
    operator_id uuid NOT NULL,
    old_state text,
    new_state text NOT NULL,
    evidence_digest bytea,
    error_code text,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT one_bootstrap_digest_is_sha256
        CHECK (evidence_digest IS NULL OR octet_length(evidence_digest)=32)
);
CREATE INDEX one_bootstrap_events_for_job
    ON one_auth.tenant_bootstrap_events(job_id,recorded_at);
REVOKE ALL ON one_auth.tenant_bootstrap_jobs FROM PUBLIC;
REVOKE ALL ON one_auth.tenant_bootstrap_events FROM PUBLIC;
-- No SELECT grants to auth lookup, report readers or provisioning role.
-- A separate restricted admin auditor account needs reviewed read-only grants.

-- Replace the unsafe prototype behavior that had marked new tenants active.
CREATE OR REPLACE FUNCTION one_auth.register_tenant(
    p_tenant uuid, p_operator uuid, p_request uuid
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, one_auth AS $$
BEGIN
    IF p_tenant IS NULL OR p_operator IS NULL OR p_request IS NULL THEN
        RAISE EXCEPTION 'Required provisioning identifier missing';
    END IF;
    INSERT INTO one_auth.provisioning_audit
        (request_id,operator_id,action,tenant_id)
    VALUES (p_request,p_operator,'register_tenant',p_tenant);
    INSERT INTO one_auth.tenant_registry(tenant_id,active)
    VALUES (p_tenant,false)
    ON CONFLICT (tenant_id) DO NOTHING;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Tenant is already registered';
    END IF;
END $$;

-- Suspending an existing tenant remains possible; reactivation must be
-- independently approved after revocation/backup/route re-verification.
CREATE OR REPLACE FUNCTION one_auth.set_tenant_active(
    p_tenant uuid, p_enabled boolean, p_operator uuid, p_request uuid
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, one_auth AS $$
BEGIN
    IF p_tenant IS NULL OR p_enabled IS DISTINCT FROM false OR
       p_operator IS NULL OR p_request IS NULL THEN
        RAISE EXCEPTION 'Only tenant suspension is allowed via this function';
    END IF;
    UPDATE one_auth.tenant_registry SET active=false WHERE tenant_id=p_tenant;
    IF NOT FOUND THEN RAISE EXCEPTION 'Unknown tenant'; END IF;
    INSERT INTO one_auth.provisioning_audit
        (request_id,operator_id,action,tenant_id,details)
    VALUES (p_request,p_operator,'set_tenant_active',p_tenant,
            jsonb_build_object('enabled',false));
END $$;

CREATE FUNCTION one_auth.begin_tenant_bootstrap(
    p_tenant uuid, p_job uuid, p_operator uuid, p_approval text, p_event uuid
) RETURNS text LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, one_auth AS $$
DECLARE existing one_auth.tenant_bootstrap_jobs%ROWTYPE;
BEGIN
    IF p_tenant IS NULL OR p_job IS NULL OR p_operator IS NULL OR
       p_approval IS NULL OR p_approval !~ '^[A-Za-z0-9._:-]{8,128}$' OR
       p_event IS NULL THEN
        RAISE EXCEPTION 'Invalid approved bootstrap request';
    END IF;

    -- Serialize claims for one tenant, including concurrent worker retries.
    PERFORM 1 FROM one_auth.tenant_registry
      WHERE tenant_id=p_tenant AND NOT active FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Unknown or active tenant';
    END IF;
    SELECT * INTO existing FROM one_auth.tenant_bootstrap_jobs
      WHERE tenant_id=p_tenant;
    IF FOUND THEN
        IF existing.job_id=p_job AND existing.operator_id=p_operator AND
           existing.approval_reference=p_approval AND
           EXISTS (SELECT 1 FROM one_auth.tenant_bootstrap_events
                   WHERE event_id=p_event AND job_id=p_job
                     AND new_state='requested') THEN
            RETURN existing.state;
        END IF;
        RAISE EXCEPTION 'Tenant provisioning job already exists';
    END IF;
    INSERT INTO one_auth.tenant_bootstrap_jobs(
        job_id,tenant_id,operator_id,approval_reference
    ) VALUES (p_job,p_tenant,p_operator,p_approval);
    INSERT INTO one_auth.tenant_bootstrap_events(
        event_id,job_id,tenant_id,operator_id,old_state,new_state
    ) VALUES (p_event,p_job,p_tenant,p_operator,NULL,'requested');
    RETURN 'requested';
END $$;

CREATE FUNCTION one_auth.advance_tenant_bootstrap(
    p_job uuid, p_operator uuid, p_next text,
    p_digest bytea, p_event uuid
) RETURNS text LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, one_auth AS $$
DECLARE j one_auth.tenant_bootstrap_jobs%ROWTYPE;
DECLARE next_expected text;
DECLARE previous_event one_auth.tenant_bootstrap_events%ROWTYPE;
BEGIN
    IF p_job IS NULL OR p_operator IS NULL OR p_next IS NULL OR
       p_event IS NULL OR p_digest IS NULL OR octet_length(p_digest)<>32 THEN
        RAISE EXCEPTION 'Invalid verified checkpoint';
    END IF;
    SELECT * INTO j FROM one_auth.tenant_bootstrap_jobs
      WHERE job_id=p_job FOR UPDATE;
    IF NOT FOUND OR j.operator_id<>p_operator THEN
        RAISE EXCEPTION 'Unknown job or operator mismatch';
    END IF;
    SELECT * INTO previous_event FROM one_auth.tenant_bootstrap_events
      WHERE event_id=p_event;
    IF FOUND THEN
        IF previous_event.job_id=p_job AND
           previous_event.operator_id=p_operator AND
           previous_event.new_state=p_next AND
           previous_event.evidence_digest=p_digest THEN
            RETURN j.state; -- exact event retry; no second side effect
        END IF;
        RAISE EXCEPTION 'Provisioning event id reused';
    END IF;
    next_expected := CASE j.state
        WHEN 'requested' THEN 'database_created'
        WHEN 'database_created' THEN 'schema_migrated'
        WHEN 'schema_migrated' THEN 'isolation_verified'
        WHEN 'isolation_verified' THEN 'restore_verified'
        WHEN 'restore_verified' THEN 'route_verified'
        WHEN 'route_verified' THEN 'ready'
        ELSE NULL
    END;
    IF next_expected IS NULL OR p_next<>next_expected THEN
        RAISE EXCEPTION 'Invalid or skipped provisioning stage';
    END IF;
    IF p_next='ready' AND EXISTS (
        SELECT 1 FROM one_auth.tenant_registry
          WHERE tenant_id=j.tenant_id AND active
    ) THEN
        RAISE EXCEPTION 'Tenant is unexpectedly active';
    END IF;
    UPDATE one_auth.tenant_bootstrap_jobs
      SET state=p_next,changed_at=clock_timestamp()
      WHERE job_id=p_job;
    INSERT INTO one_auth.tenant_bootstrap_events (
        event_id,job_id,tenant_id,operator_id,old_state,new_state,evidence_digest
    ) VALUES(p_event,p_job,j.tenant_id,p_operator,j.state,p_next,p_digest);
    IF p_next='ready' THEN
        UPDATE one_auth.tenant_registry SET active=true
          WHERE tenant_id=j.tenant_id AND NOT active;
        IF NOT FOUND THEN RAISE EXCEPTION 'Registry activation conflict'; END IF;
    END IF;
    RETURN p_next;
END $$;

CREATE FUNCTION one_auth.fail_tenant_bootstrap(
    p_job uuid, p_operator uuid, p_code text, p_event uuid
) RETURNS text LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, one_auth AS $$
DECLARE j one_auth.tenant_bootstrap_jobs%ROWTYPE;
DECLARE previous_event one_auth.tenant_bootstrap_events%ROWTYPE;
BEGIN
    IF p_job IS NULL OR p_operator IS NULL OR p_code IS NULL OR
       p_code !~ '^[A-Z_]{3,48}$' OR p_event IS NULL THEN
        RAISE EXCEPTION 'Invalid sanitized bootstrap failure code';
    END IF;
    SELECT * INTO j FROM one_auth.tenant_bootstrap_jobs
      WHERE job_id=p_job FOR UPDATE;
    IF NOT FOUND OR j.operator_id<>p_operator THEN
        RAISE EXCEPTION 'Unknown job or operator mismatch';
    END IF;
    SELECT * INTO previous_event FROM one_auth.tenant_bootstrap_events
      WHERE event_id=p_event;
    IF FOUND THEN
        IF previous_event.job_id=p_job AND
           previous_event.operator_id=p_operator AND
           previous_event.error_code=p_code AND
           previous_event.new_state='failed' THEN
            RETURN j.state;
        END IF;
        RAISE EXCEPTION 'Provisioning failure event id reused';
    END IF;
    IF j.state IN ('failed','ready') THEN
        RAISE EXCEPTION 'Terminal provisioning state cannot be overwritten';
    END IF;
    UPDATE one_auth.tenant_bootstrap_jobs
      SET state='failed',changed_at=clock_timestamp() WHERE job_id=p_job;
    UPDATE one_auth.tenant_registry SET active=false
      WHERE tenant_id=j.tenant_id;
    INSERT INTO one_auth.tenant_bootstrap_events(
        event_id,job_id,tenant_id,operator_id,old_state,new_state,error_code
    ) VALUES (p_event,p_job,j.tenant_id,p_operator,j.state,'failed',p_code);
    RETURN 'failed';
END $$;

-- Ensure even application roles cannot rewrite the audit trail.
CREATE FUNCTION one_auth.reject_bootstrap_event_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Bootstrap events are append only'; END $$;
CREATE TRIGGER bootstrap_event_immutable
BEFORE UPDATE OR DELETE ON one_auth.tenant_bootstrap_events
FOR EACH ROW EXECUTE FUNCTION one_auth.reject_bootstrap_event_mutation();

REVOKE ALL ON FUNCTION one_auth.begin_tenant_bootstrap(
    uuid,uuid,uuid,text,uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION one_auth.advance_tenant_bootstrap(
    uuid,uuid,text,bytea,uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION one_auth.fail_tenant_bootstrap(
    uuid,uuid,text,uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION one_auth.reject_bootstrap_event_mutation() FROM PUBLIC;

GRANT EXECUTE ON FUNCTION one_auth.begin_tenant_bootstrap(
    uuid,uuid,uuid,text,uuid) TO one_erp_auth_provisioner;
GRANT EXECUTE ON FUNCTION one_auth.advance_tenant_bootstrap(
    uuid,uuid,text,bytea,uuid) TO one_erp_auth_provisioner;
GRANT EXECUTE ON FUNCTION one_auth.fail_tenant_bootstrap(
    uuid,uuid,text,uuid) TO one_erp_auth_provisioner;

COMMIT;
-- WARNING: Evidence digests only prove recorded values, not independently
-- authenticated evidence! A *trusted backend* MUST verify DB identity,
-- migration versions, cross-DB isolation, actual encrypted restore, TLS
-- certificate/routing and final release approval BEFORE advancing stages.
-- No true credential vault, backup, IdP, or HTTPS checker exists in this SQL.
