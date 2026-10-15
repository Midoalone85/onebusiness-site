-- ONE ERP NEXT / 048 | Tenant/user-aware finance permission decisions.
-- Independently approved per-user / per-role allow/deny overrides, not UI-only.
-- Enforcement is database-side for 046 date attestation, 047 enrollment,
-- and ALL period close updates, including 045 legacy close.
-- Current PostgreSQL GUCs are trusted-backend contract, NOT authentication.
-- Service must deny arbitrary SQL to users; CI broad DML is NOT production.
BEGIN;

CREATE TABLE one_permission_templates (
 permission_key text NOT NULL,
 role text NOT NULL CHECK(role IN ('company_admin','accountant','viewer')),
 default_allow boolean NOT NULL,
 risk_class text NOT NULL CHECK(risk_class IN ('read','finance','high')),
 PRIMARY KEY(permission_key,role),
 CHECK(permission_key ~ '^[a-z][a-z0-9_.]{4,99}$')
);
INSERT INTO one_permission_templates(permission_key,role,default_allow,risk_class)
SELECT permission_key,r,allowed,risk FROM (VALUES
 ('finance.period.hard_close','high',true,false,false),
 ('finance.period.soft_adjust','finance',true,true,false),
 ('finance.inventory.history.activate.request','high',true,false,false),
 ('finance.inventory.history.activate.approve','high',true,true,false),
 ('finance.inventory.history.view','read',true,true,true),
 ('inventory.date.request','finance',true,false,false),
 ('inventory.date.approve','finance',true,true,false),
 ('finance.permission.change.request','high',true,false,false),
 ('finance.permission.change.approve','high',true,true,false)
) t(permission_key,risk,admin,accountant,viewer)
CROSS JOIN LATERAL (VALUES
 ('company_admin',t.admin),
 ('accountant',t.accountant),
 ('viewer',t.viewer)
) roles(r,allowed);

CREATE TABLE one_permission_change_requests (
 tenant_id uuid NOT NULL REFERENCES one_tenants(id),
 id uuid NOT NULL,
 target_actor_id uuid,
 target_role text CHECK(target_role IN ('company_admin','accountant','viewer')),
 permission_key text NOT NULL,
 decision text NOT NULL CHECK(decision IN ('allow','deny')),
 effective_from date NOT NULL,
 effective_until date,
 requested_by uuid NOT NULL,
 requested_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 PRIMARY KEY(tenant_id,id),
 FOREIGN KEY(permission_key,target_role)
  REFERENCES one_permission_templates(permission_key,role),
 CHECK((target_actor_id IS NULL)<>(target_role IS NULL)),
 CHECK(effective_until IS NULL OR effective_until>effective_from)
);
-- Role grants need a template-role FK, while actor overrides need permission
-- key alone. The template catalog's permission keys are unique through
-- their separately indexed catalog of known action keys.
CREATE TABLE one_permission_keys(
 permission_key text PRIMARY KEY
   CHECK(permission_key ~ '^[a-z][a-z0-9_.]{4,99}$')
);
INSERT INTO one_permission_keys(permission_key)
 SELECT DISTINCT permission_key FROM one_permission_templates;
ALTER TABLE one_permission_change_requests
 DROP CONSTRAINT one_permission_change_requests_permission_key_target_role_fkey;
ALTER TABLE one_permission_change_requests
 ADD CONSTRAINT one_permission_change_requests_permission_key_fkey
 FOREIGN KEY(permission_key) REFERENCES one_permission_keys(permission_key);

CREATE TABLE one_permission_change_approvals (
 tenant_id uuid NOT NULL,
 request_id uuid NOT NULL,
 approved_by uuid NOT NULL,
 approved_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 sequence_no bigint GENERATED ALWAYS AS IDENTITY,
 PRIMARY KEY(tenant_id,request_id),
 UNIQUE(sequence_no),
 FOREIGN KEY(tenant_id,request_id)
   REFERENCES one_permission_change_requests(tenant_id,id)
);
ALTER TABLE one_permission_change_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_permission_change_requests FORCE ROW LEVEL SECURITY;
CREATE POLICY one_permission_proposal_scope ON one_permission_change_requests
 USING(tenant_id::text=current_setting('one.tenant_id',true))
 WITH CHECK(tenant_id::text=current_setting('one.tenant_id',true));
ALTER TABLE one_permission_change_approvals ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_permission_change_approvals FORCE ROW LEVEL SECURITY;
CREATE POLICY one_permission_approval_scope ON one_permission_change_approvals
 USING(tenant_id::text=current_setting('one.tenant_id',true))
 WITH CHECK(tenant_id::text=current_setting('one.tenant_id',true));

-- All decisions and proposals are immutable; write only from the reviewed
-- service interface. A privileged SQL editor can impersonate GUCs; do NOT
-- grant DML/SET arbitrary session identity outside the trusted backend.
CREATE FUNCTION one_permission_fact_guard() RETURNS trigger LANGUAGE plpgsql AS $permission_guard$
BEGIN
 IF TG_OP<>'INSERT' OR
   current_setting('one.permission_admin_write',true) IS DISTINCT FROM 'yes' THEN
  RAISE EXCEPTION 'Permission policy changes require two-step audited workflow';
 END IF;
 RETURN NEW;
END $permission_guard$;
CREATE TRIGGER one_permission_proposal_guard
 BEFORE INSERT OR UPDATE OR DELETE ON one_permission_change_requests
 FOR EACH ROW EXECUTE FUNCTION one_permission_fact_guard();
CREATE TRIGGER one_permission_decision_guard
 BEFORE INSERT OR UPDATE OR DELETE ON one_permission_change_approvals
 FOR EACH ROW EXECUTE FUNCTION one_permission_fact_guard();

-- This is the common authorization primitive for all next-generation
-- financial workflows. Explicit per-USER policy takes precedence over role
-- policy, and most recent approved rule wins at the same specificity.
-- An active membership, tenant context and actor context are ALL required.
CREATE FUNCTION one_actor_has_permission(
 p_tenant uuid,p_actor uuid,p_permission text
) RETURNS boolean LANGUAGE plpgsql STABLE SECURITY INVOKER AS $permission_eval$
DECLARE user_role text;
 result boolean;
BEGIN
 IF p_tenant IS NULL OR p_actor IS NULL OR p_permission IS NULL OR
    current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
    current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text THEN
  RETURN false;
 END IF;
 SELECT m.role INTO user_role FROM one_memberships m
  WHERE m.tenant_id=p_tenant AND m.actor_id=p_actor AND m.active;
 IF user_role IS NULL THEN RETURN false; END IF;
 SELECT (r.decision='allow') INTO result
 FROM one_permission_change_requests r
 JOIN one_permission_change_approvals a
   ON a.tenant_id=r.tenant_id AND a.request_id=r.id
 WHERE r.tenant_id=p_tenant AND r.permission_key=p_permission
   AND (r.target_actor_id=p_actor OR r.target_role=user_role)
   AND r.effective_from<=current_date
   AND (r.effective_until IS NULL OR r.effective_until>current_date)
 ORDER BY (r.target_actor_id IS NOT NULL) DESC,a.sequence_no DESC
 LIMIT 1;
 IF FOUND THEN RETURN result; END IF;
 SELECT t.default_allow INTO result FROM one_permission_templates t
  WHERE t.permission_key=p_permission AND t.role=user_role;
 RETURN coalesce(result,false);
END $permission_eval$;

CREATE FUNCTION one_request_permission_change(
 p_tenant uuid,p_request uuid,p_target_actor uuid,p_target_role text,
 p_permission text,p_decision text,p_from date,p_until date,p_actor uuid
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $make_permission_request$
BEGIN
 IF p_tenant IS NULL OR p_request IS NULL OR p_actor IS NULL OR
    p_from IS NULL OR p_decision NOT IN ('allow','deny') OR
    (p_target_actor IS NULL)=(p_target_role IS NULL) OR
    (p_until IS NOT NULL AND p_until<=p_from) OR
    current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
    current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
    NOT EXISTS(SELECT 1 FROM one_memberships m
      WHERE m.tenant_id=p_tenant AND m.actor_id=p_actor AND m.active
        AND m.role='company_admin') OR
    NOT EXISTS(SELECT 1 FROM one_permission_keys k
      WHERE k.permission_key=p_permission) THEN
  RAISE EXCEPTION 'Authorized and valid permission proposal required';
 END IF;
 IF p_target_actor IS NOT NULL AND NOT EXISTS(
    SELECT 1 FROM one_memberships m
    WHERE m.tenant_id=p_tenant AND m.actor_id=p_target_actor) THEN
  RAISE EXCEPTION 'Permission target user does not belong to tenant';
 END IF;
 PERFORM set_config('one.permission_admin_write','yes',true);
 INSERT INTO one_permission_change_requests(
  tenant_id,id,target_actor_id,target_role,permission_key,decision,
  effective_from,effective_until,requested_by)
 VALUES(p_tenant,p_request,p_target_actor,p_target_role,p_permission,
  p_decision,p_from,p_until,p_actor);
 PERFORM set_config('one.permission_admin_write','',true);
 RETURN p_request;
END $make_permission_request$;

CREATE FUNCTION one_approve_permission_change(
 p_tenant uuid,p_request uuid,p_actor uuid
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $review_permission_request$
DECLARE proposed one_permission_change_requests%ROWTYPE;
BEGIN
 IF p_tenant IS NULL OR p_request IS NULL OR p_actor IS NULL OR
   current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
   current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
   NOT EXISTS(SELECT 1 FROM one_memberships m
      WHERE m.tenant_id=p_tenant AND m.actor_id=p_actor AND m.active
        AND m.role IN ('company_admin','accountant')) THEN
  RAISE EXCEPTION 'An authenticated independent policy reviewer is required';
 END IF;
 SELECT * INTO proposed FROM one_permission_change_requests
   WHERE tenant_id=p_tenant AND id=p_request FOR UPDATE;
 IF NOT FOUND OR proposed.requested_by=p_actor OR
   NOT EXISTS(SELECT 1 FROM one_memberships m
     WHERE m.tenant_id=p_tenant AND m.actor_id=proposed.requested_by
       AND m.role='company_admin' AND m.active) THEN
  RAISE EXCEPTION 'Permission policy needs independent active requester/reviewer';
 END IF;
 PERFORM set_config('one.permission_admin_write','yes',true);
 INSERT INTO one_permission_change_approvals(
  tenant_id,request_id,approved_by)
 VALUES(p_tenant,p_request,p_actor);
 PERFORM set_config('one.permission_admin_write','',true);
 RETURN p_request;
END $review_permission_request$;

-- These AFTER/BEFORE hooks guard ALL existing operations, including old
-- paths which are still hard-coded to legacy company-admin/accountant roles.
-- An explicit policy DENY is effective immediately. Policy ALLOW does NOT
-- overrule legacy role checks inside each existing downstream function.
CREATE FUNCTION one_permission_period_close_guard() RETURNS trigger LANGUAGE plpgsql AS $close_guard$
BEGIN
 IF OLD.closed=false AND NEW.closed=true AND
    NOT one_actor_has_permission(
      NEW.tenant_id,
      nullif(current_setting('one.actor_id',true),'')::uuid,
      'finance.period.hard_close') THEN
  RAISE EXCEPTION 'Finance period close denied by user permission policy';
 END IF;
 RETURN NEW;
END $close_guard$;
CREATE TRIGGER one_permission_on_period_close
 BEFORE UPDATE OF closed ON one_periods
 FOR EACH ROW EXECUTE FUNCTION one_permission_period_close_guard();

CREATE FUNCTION one_permission_request_inventory_date_guard()
 RETURNS trigger LANGUAGE plpgsql AS $date_request_guard$
BEGIN
 IF NOT one_actor_has_permission(
   NEW.tenant_id,NEW.requested_by,'inventory.date.request') THEN
  RAISE EXCEPTION 'Inventory date request denied by user permissions';
 END IF;
 RETURN NEW;
END $date_request_guard$;
CREATE TRIGGER one_permission_on_inventory_date_request
 BEFORE INSERT ON one_inventory_date_requests
 FOR EACH ROW EXECUTE FUNCTION one_permission_request_inventory_date_guard();

CREATE FUNCTION one_permission_approved_inventory_date_guard()
 RETURNS trigger LANGUAGE plpgsql AS $date_approval_guard$
BEGIN
 IF NOT one_actor_has_permission(
  NEW.tenant_id,NEW.approved_by,'inventory.date.approve') THEN
  RAISE EXCEPTION 'Inventory date approval denied by user permissions';
 END IF;
 RETURN NEW;
END $date_approval_guard$;
CREATE TRIGGER one_permission_on_inventory_date_approval
 BEFORE INSERT ON one_inventory_effective_dates
 FOR EACH ROW EXECUTE FUNCTION one_permission_approved_inventory_date_guard();

CREATE FUNCTION one_permission_historical_enrollment_guard()
 RETURNS trigger LANGUAGE plpgsql AS $activation_gate$
DECLARE p_key text; p_user uuid;
BEGIN
 IF TG_TABLE_NAME='one_historical_close_activation_requests' THEN
  p_key='finance.inventory.history.activate.request';
  p_user=NEW.requested_by;
 ELSE
  p_key='finance.inventory.history.activate.approve';
  p_user=NEW.approved_by;
 END IF;
 IF NOT one_actor_has_permission(NEW.tenant_id,p_user,p_key) THEN
  RAISE EXCEPTION 'Historical close activation denied by user permissions';
 END IF;
 RETURN NEW;
END $activation_gate$;
CREATE TRIGGER one_permission_on_historical_activation_request
 BEFORE INSERT ON one_historical_close_activation_requests
 FOR EACH ROW EXECUTE FUNCTION one_permission_historical_enrollment_guard();
CREATE TRIGGER one_permission_on_historical_activation_approval
 BEFORE INSERT ON one_historical_close_enrollments
 FOR EACH ROW EXECUTE FUNCTION one_permission_historical_enrollment_guard();

-- The permission templates and catalog are maintained by trusted migrations,
-- not tenant users. Tenant policy changes are append-only with independent
-- approval, and no direct record edits or mutable history.
REVOKE ALL ON one_permission_templates,one_permission_keys,
 one_permission_change_requests,one_permission_change_approvals FROM PUBLIC;
REVOKE ALL ON FUNCTION one_actor_has_permission(uuid,uuid,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION one_request_permission_change(
 uuid,uuid,uuid,text,text,text,date,date,uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION one_approve_permission_change(uuid,uuid,uuid) FROM PUBLIC;
COMMIT;
