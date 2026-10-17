-- ONE ERP Next control-plane provisioning, draft only (PostgreSQL 14+).
-- Apply ONLY to an isolated control-plane database after independent backup.
-- 001_identity_registry.sql must already be applied.
-- No public HTTP admin endpoint, actual IdP onboarding, or live tenant DB
-- provisioning is included. Tenant registration is a logical allowlist only.
BEGIN;

CREATE TABLE one_auth.tenant_registry (
    tenant_id uuid PRIMARY KEY,
    active boolean NOT NULL DEFAULT true,
    registered_at timestamptz NOT NULL DEFAULT now()
);

-- Every privileged transition records its own immutable audit event in the
-- same transaction. request_id uniqueness rejects replays before commit.
-- operator_id must be injected by the trusted, authenticated admin backend.
CREATE TABLE one_auth.provisioning_audit (
    request_id uuid PRIMARY KEY,
    operator_id uuid NOT NULL,
    action text NOT NULL CHECK (action IN (
        'register_tenant', 'set_tenant_active', 'link_subject',
        'disable_subject', 'set_entitlement', 'revoke_jti'
    )),
    actor_id uuid,
    tenant_id uuid,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    details jsonb NOT NULL DEFAULT '{}'::jsonb
);
REVOKE ALL ON one_auth.tenant_registry FROM PUBLIC;
REVOKE ALL ON one_auth.provisioning_audit FROM PUBLIC;

-- Auth lookup is read-only. A disabled logical tenant cannot be authorized.
GRANT SELECT ON one_auth.tenant_registry TO one_erp_auth_lookup;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='one_erp_auth_provisioner') THEN
        CREATE ROLE one_erp_auth_provisioner
            NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION;
    END IF;
END $$;
GRANT USAGE ON SCHEMA one_auth TO one_erp_auth_provisioner;

CREATE FUNCTION one_auth.register_tenant(
    p_tenant uuid, p_operator uuid, p_request uuid
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, one_auth AS $$
BEGIN
    IF p_tenant IS NULL OR p_operator IS NULL OR p_request IS NULL THEN
        RAISE EXCEPTION 'Required provisioning identifier missing';
    END IF;
    INSERT INTO one_auth.provisioning_audit
        (request_id, operator_id, action, tenant_id)
    VALUES (p_request, p_operator, 'register_tenant', p_tenant);
    INSERT INTO one_auth.tenant_registry (tenant_id, active)
    VALUES (p_tenant, true)
    ON CONFLICT (tenant_id) DO UPDATE SET active=true;
END $$;

CREATE FUNCTION one_auth.set_tenant_active(
    p_tenant uuid, p_enabled boolean, p_operator uuid, p_request uuid
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, one_auth AS $$
BEGIN
    IF p_tenant IS NULL OR p_enabled IS NULL OR
       p_operator IS NULL OR p_request IS NULL THEN
        RAISE EXCEPTION 'Required provisioning argument missing';
    END IF;
    INSERT INTO one_auth.provisioning_audit
        (request_id, operator_id, action, tenant_id, details)
    VALUES (p_request, p_operator, 'set_tenant_active', p_tenant,
            jsonb_build_object('enabled',p_enabled));
    UPDATE one_auth.tenant_registry SET active=p_enabled WHERE tenant_id=p_tenant;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Unknown tenant';
    END IF;
END $$;

CREATE FUNCTION one_auth.link_subject(
    p_issuer text, p_subject text, p_actor uuid,
    p_operator uuid, p_request uuid
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, one_auth AS $$
DECLARE old_actor uuid;
BEGIN
    IF p_issuer IS NULL OR p_issuer !~ '^https://' OR
       length(p_issuer)>2048 OR p_subject IS NULL OR
       length(p_subject) NOT BETWEEN 1 AND 255 OR
       p_actor IS NULL OR p_operator IS NULL OR p_request IS NULL THEN
        RAISE EXCEPTION 'Invalid subject provisioning';
    END IF;
    -- Never rebind an existing IdP identity to a different internal actor.
    SELECT actor_id INTO old_actor FROM one_auth.subject_links
       WHERE issuer=p_issuer AND subject=p_subject FOR UPDATE;
    IF old_actor IS NOT NULL AND old_actor<>p_actor THEN
        RAISE EXCEPTION 'Subject already belongs to another actor';
    END IF;
    INSERT INTO one_auth.provisioning_audit
        (request_id, operator_id, action, actor_id, details)
    VALUES (p_request, p_operator, 'link_subject', p_actor,
            jsonb_build_object('issuer',p_issuer));
    INSERT INTO one_auth.subject_links (issuer,subject,actor_id,active)
    VALUES (p_issuer,p_subject,p_actor,true)
    ON CONFLICT (issuer,subject) DO UPDATE SET active=true
        WHERE subject_links.actor_id=EXCLUDED.actor_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Subject already belongs to another actor';
    END IF;
END $$;

CREATE FUNCTION one_auth.disable_subject(
    p_issuer text, p_subject text, p_operator uuid, p_request uuid
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, one_auth AS $$
DECLARE v_actor uuid;
BEGIN
    IF p_issuer IS NULL OR p_subject IS NULL OR
       p_operator IS NULL OR p_request IS NULL THEN
        RAISE EXCEPTION 'Required subject identifier missing';
    END IF;
    SELECT actor_id INTO v_actor FROM one_auth.subject_links
       WHERE issuer=p_issuer AND subject=p_subject FOR UPDATE;
    IF v_actor IS NULL THEN
        RAISE EXCEPTION 'Unknown subject';
    END IF;
    INSERT INTO one_auth.provisioning_audit
        (request_id,operator_id,action,actor_id)
    VALUES (p_request,p_operator,'disable_subject',v_actor);
    UPDATE one_auth.subject_links SET active=false
       WHERE issuer=p_issuer AND subject=p_subject;
END $$;

CREATE FUNCTION one_auth.set_entitlement(
    p_actor uuid, p_tenant uuid, p_enabled boolean,
    p_operator uuid, p_request uuid
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, one_auth AS $$
BEGIN
    IF p_actor IS NULL OR p_tenant IS NULL OR p_enabled IS NULL OR
       p_operator IS NULL OR p_request IS NULL THEN
        RAISE EXCEPTION 'Required entitlement argument missing';
    END IF;
    IF p_enabled AND NOT EXISTS (
        SELECT 1 FROM one_auth.tenant_registry
          WHERE tenant_id=p_tenant AND active
    ) THEN
        RAISE EXCEPTION 'Unregistered or inactive tenant';
    END IF;
    IF p_enabled AND NOT EXISTS (
        SELECT 1 FROM one_auth.subject_links
          WHERE actor_id=p_actor AND active
    ) THEN
        RAISE EXCEPTION 'Actor has no active trusted identity';
    END IF;
    INSERT INTO one_auth.provisioning_audit
        (request_id,operator_id,action,actor_id,tenant_id,details)
    VALUES (p_request,p_operator,'set_entitlement',p_actor,p_tenant,
            jsonb_build_object('enabled',p_enabled));
    IF p_enabled THEN
        INSERT INTO one_auth.tenant_entitlements (actor_id,tenant_id,active)
        VALUES (p_actor,p_tenant,true)
        ON CONFLICT (actor_id,tenant_id) DO UPDATE SET active=true;
    ELSE
        UPDATE one_auth.tenant_entitlements SET active=false
        WHERE actor_id=p_actor AND tenant_id=p_tenant;
    END IF;
END $$;

CREATE FUNCTION one_auth.revoke_jti_digest(
    p_digest bytea, p_expires_at timestamptz,
    p_operator uuid, p_request uuid
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, one_auth AS $$
BEGIN
    IF p_digest IS NULL OR octet_length(p_digest)<>32 OR
       p_expires_at IS NULL OR p_expires_at<=now() OR
       p_operator IS NULL OR p_request IS NULL THEN
        RAISE EXCEPTION 'Invalid revocation';
    END IF;
    INSERT INTO one_auth.provisioning_audit
        (request_id,operator_id,action)
    VALUES (p_request,p_operator,'revoke_jti');
    INSERT INTO one_auth.revoked_jti(token_digest,expires_at)
    VALUES (p_digest,p_expires_at)
    ON CONFLICT (token_digest) DO UPDATE
        SET expires_at=GREATEST(
            one_auth.revoked_jti.expires_at,EXCLUDED.expires_at
        );
END $$;

REVOKE ALL ON FUNCTION one_auth.register_tenant(uuid,uuid,uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION one_auth.set_tenant_active(uuid,boolean,uuid,uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION one_auth.link_subject(text,text,uuid,uuid,uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION one_auth.disable_subject(text,text,uuid,uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION one_auth.set_entitlement(uuid,uuid,boolean,uuid,uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION one_auth.revoke_jti_digest(bytea,timestamptz,uuid,uuid) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION one_auth.register_tenant(uuid,uuid,uuid) TO one_erp_auth_provisioner;
GRANT EXECUTE ON FUNCTION one_auth.set_tenant_active(uuid,boolean,uuid,uuid) TO one_erp_auth_provisioner;
GRANT EXECUTE ON FUNCTION one_auth.link_subject(text,text,uuid,uuid,uuid) TO one_erp_auth_provisioner;
GRANT EXECUTE ON FUNCTION one_auth.disable_subject(text,text,uuid,uuid) TO one_erp_auth_provisioner;
GRANT EXECUTE ON FUNCTION one_auth.set_entitlement(uuid,uuid,boolean,uuid,uuid) TO one_erp_auth_provisioner;
GRANT EXECUTE ON FUNCTION one_auth.revoke_jti_digest(bytea,timestamptz,uuid,uuid) TO one_erp_auth_provisioner;
COMMIT;

-- Do not grant one_erp_auth_provisioner to a browser or tenant DB role.
-- API authentication/authorizations, dual approvals and external audit shipping
-- remain production release gates. NEVER pass operator_id from user input.
