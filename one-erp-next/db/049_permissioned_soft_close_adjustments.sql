-- ONE ERP NEXT / 049 | Financial soft close + double-reviewed late adjustments.
-- PostgreSQL 14 pilot, not a deployed application. Roles are resolved through
-- 048 per-user/per-role permissions. Legacy hard close remains immutable.
-- A soft-close marker locks ordinary GL posting; only independently approved
-- NEW two-line balancing journals may enter a soft-closed period.
-- This pilot does NOT reopen hard close, recast tax invoices or authenticate
-- raw SQL sessions. Production requires trusted actor-bound service API.
BEGIN;

INSERT INTO one_permission_templates(permission_key,role,default_allow,risk_class)
SELECT p.permission_key,roles.r,roles.allowed,p.risk FROM (VALUES
 ('finance.period.soft_close.request','finance',true,false,false),
 ('finance.period.soft_close.approve','high',true,true,false),
 ('finance.period.soft_close.view','read',true,true,true),
 ('finance.period.soft_adjust.approve','high',true,true,false)
) p(permission_key,risk,admin,accountant,viewer)
CROSS JOIN LATERAL (VALUES
 ('company_admin',p.admin),('accountant',p.accountant),('viewer',p.viewer)
) roles(r,allowed);
INSERT INTO one_permission_keys(permission_key)
VALUES('finance.period.soft_close.request'),('finance.period.soft_close.approve'),
 ('finance.period.soft_close.view'),('finance.period.soft_adjust.approve');

CREATE TABLE one_period_soft_close_requests(
 tenant_id uuid NOT NULL,
 period_id uuid NOT NULL,
 requested_by uuid NOT NULL,
 requested_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 PRIMARY KEY(tenant_id,period_id),
 FOREIGN KEY(tenant_id,period_id) REFERENCES one_periods(tenant_id,id)
);
CREATE TABLE one_period_soft_close_approvals(
 tenant_id uuid NOT NULL,
 period_id uuid NOT NULL,
 approved_by uuid NOT NULL,
 approved_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 PRIMARY KEY(tenant_id,period_id),
 FOREIGN KEY(tenant_id,period_id)
  REFERENCES one_period_soft_close_requests(tenant_id,period_id)
);
CREATE TABLE one_soft_adjustment_requests(
 tenant_id uuid NOT NULL,
 id uuid NOT NULL,
 period_id uuid NOT NULL,
 journal_id uuid NOT NULL,
 requested_by uuid NOT NULL,
 reason text NOT NULL CHECK(length(btrim(reason)) BETWEEN 10 AND 500),
 requested_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 PRIMARY KEY(tenant_id,id),
 UNIQUE(tenant_id,journal_id),
 FOREIGN KEY(tenant_id,period_id)
  REFERENCES one_period_soft_close_approvals(tenant_id,period_id),
 FOREIGN KEY(tenant_id,journal_id)
  REFERENCES one_journal_entries(tenant_id,id)
);
CREATE TABLE one_soft_adjustment_approvals(
 tenant_id uuid NOT NULL,
 request_id uuid NOT NULL,
 approved_by uuid NOT NULL,
 approved_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 PRIMARY KEY(tenant_id,request_id),
 FOREIGN KEY(tenant_id,request_id)
  REFERENCES one_soft_adjustment_requests(tenant_id,id)
);
CREATE TABLE one_soft_adjustment_post_audit(
 tenant_id uuid NOT NULL,
 request_id uuid NOT NULL,
 journal_id uuid NOT NULL,
 posted_by uuid NOT NULL,
 posted_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 PRIMARY KEY(tenant_id,request_id),
 UNIQUE(tenant_id,journal_id),
 FOREIGN KEY(tenant_id,request_id)
  REFERENCES one_soft_adjustment_approvals(tenant_id,request_id),
 FOREIGN KEY(tenant_id,journal_id)
  REFERENCES one_journal_entries(tenant_id,id)
);
ALTER TABLE one_period_soft_close_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_period_soft_close_requests FORCE ROW LEVEL SECURITY;
CREATE POLICY one_soft_close_request_scope ON one_period_soft_close_requests
 USING(tenant_id::text=current_setting('one.tenant_id',true))
 WITH CHECK(tenant_id::text=current_setting('one.tenant_id',true));
ALTER TABLE one_period_soft_close_approvals ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_period_soft_close_approvals FORCE ROW LEVEL SECURITY;
CREATE POLICY one_soft_close_approval_scope ON one_period_soft_close_approvals
 USING(tenant_id::text=current_setting('one.tenant_id',true))
 WITH CHECK(tenant_id::text=current_setting('one.tenant_id',true));
ALTER TABLE one_soft_adjustment_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_soft_adjustment_requests FORCE ROW LEVEL SECURITY;
CREATE POLICY one_soft_adjustment_request_scope ON one_soft_adjustment_requests
 USING(tenant_id::text=current_setting('one.tenant_id',true))
 WITH CHECK(tenant_id::text=current_setting('one.tenant_id',true));
ALTER TABLE one_soft_adjustment_approvals ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_soft_adjustment_approvals FORCE ROW LEVEL SECURITY;
CREATE POLICY one_soft_adjustment_approval_scope ON one_soft_adjustment_approvals
 USING(tenant_id::text=current_setting('one.tenant_id',true))
 WITH CHECK(tenant_id::text=current_setting('one.tenant_id',true));
ALTER TABLE one_soft_adjustment_post_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_soft_adjustment_post_audit FORCE ROW LEVEL SECURITY;
CREATE POLICY one_soft_adjustment_post_scope ON one_soft_adjustment_post_audit
 USING(tenant_id::text=current_setting('one.tenant_id',true))
 WITH CHECK(tenant_id::text=current_setting('one.tenant_id',true));

-- All workflow facts are append-only, including initial proposals. Only
-- approved trusted invoker operations may CREATE them.
CREATE FUNCTION one_soft_workflow_fact_guard() RETURNS trigger LANGUAGE plpgsql AS $soft_fact$
BEGIN
 IF TG_OP<>'INSERT' OR
    current_setting('one.soft_workflow_write',true) IS DISTINCT FROM 'yes' THEN
  RAISE EXCEPTION 'Soft-close accounting evidence is append-only; use workflow API';
 END IF;
 RETURN NEW;
END $soft_fact$;
CREATE TRIGGER one_soft_close_request_fact BEFORE INSERT OR UPDATE OR DELETE
 ON one_period_soft_close_requests FOR EACH ROW
 EXECUTE FUNCTION one_soft_workflow_fact_guard();
CREATE TRIGGER one_soft_close_approval_fact BEFORE INSERT OR UPDATE OR DELETE
 ON one_period_soft_close_approvals FOR EACH ROW
 EXECUTE FUNCTION one_soft_workflow_fact_guard();
CREATE TRIGGER one_soft_adjust_request_fact BEFORE INSERT OR UPDATE OR DELETE
 ON one_soft_adjustment_requests FOR EACH ROW
 EXECUTE FUNCTION one_soft_workflow_fact_guard();
CREATE TRIGGER one_soft_adjust_approval_fact BEFORE INSERT OR UPDATE OR DELETE
 ON one_soft_adjustment_approvals FOR EACH ROW
 EXECUTE FUNCTION one_soft_workflow_fact_guard();
CREATE TRIGGER one_soft_adjust_post_fact BEFORE INSERT OR UPDATE OR DELETE
 ON one_soft_adjustment_post_audit FOR EACH ROW
 EXECUTE FUNCTION one_soft_workflow_fact_guard();

CREATE FUNCTION one_request_period_soft_close(
 p_tenant uuid,p_period uuid,p_actor uuid
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $soft_request$
DECLARE p one_periods%ROWTYPE;
BEGIN
 IF NOT one_actor_has_permission(
   p_tenant,p_actor,'finance.period.soft_close.request') THEN
  RAISE EXCEPTION 'User may not request soft close';
 END IF;
 SELECT * INTO p FROM one_periods
  WHERE tenant_id=p_tenant AND id=p_period FOR UPDATE;
 IF NOT FOUND OR p.closed THEN
  RAISE EXCEPTION 'Only an open period can request soft close';
 END IF;
 IF EXISTS(SELECT 1 FROM one_journal_entries j WHERE
   j.tenant_id=p_tenant AND j.period_id=p_period AND j.status<>'posted') THEN
  RAISE EXCEPTION 'Draft journals must be resolved before soft close request';
 END IF;
 PERFORM set_config('one.soft_workflow_write','yes',true);
 INSERT INTO one_period_soft_close_requests(tenant_id,period_id,requested_by)
 VALUES(p_tenant,p_period,p_actor);
 PERFORM set_config('one.soft_workflow_write','',true);
 RETURN p_period;
END $soft_request$;

CREATE FUNCTION one_approve_period_soft_close(
 p_tenant uuid,p_period uuid,p_actor uuid
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $soft_approve$
DECLARE p one_periods%ROWTYPE;
 requester uuid;
BEGIN
 IF NOT one_actor_has_permission(
   p_tenant,p_actor,'finance.period.soft_close.approve') THEN
  RAISE EXCEPTION 'User may not independently approve soft close';
 END IF;
 SELECT * INTO p FROM one_periods
  WHERE tenant_id=p_tenant AND id=p_period FOR UPDATE;
 IF NOT FOUND OR p.closed THEN
  RAISE EXCEPTION 'Hard-closed periods cannot be soft-closed';
 END IF;
 SELECT requested_by INTO requester FROM one_period_soft_close_requests
 WHERE tenant_id=p_tenant AND period_id=p_period FOR UPDATE;
 IF NOT FOUND OR requester=p_actor OR NOT EXISTS(
   SELECT 1 FROM one_memberships m WHERE m.tenant_id=p_tenant
     AND m.actor_id=requester AND m.active) THEN
  RAISE EXCEPTION 'Independent active soft-close requester required';
 END IF;
 IF EXISTS(SELECT 1 FROM one_journal_entries j WHERE
   j.tenant_id=p_tenant AND j.period_id=p_period AND j.status<>'posted') THEN
  RAISE EXCEPTION 'Draft journals must be resolved before soft close';
 END IF;
 PERFORM set_config('one.soft_workflow_write','yes',true);
 INSERT INTO one_period_soft_close_approvals(tenant_id,period_id,approved_by)
 VALUES(p_tenant,p_period,p_actor);
 PERFORM set_config('one.soft_workflow_write','',true);
 RETURN p_period;
END $soft_approve$;

-- Explicit service API builds exactly two balanced journal lines atomically;
-- the ordinary backdated-entry INSERT route is blocked after soft close.
CREATE FUNCTION one_request_soft_adjustment(
 p_tenant uuid,p_request uuid,p_period uuid,p_journal uuid,p_on date,
 p_currency text,p_debit text,p_credit text,p_amount numeric,
 p_reason text,p_actor uuid
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $soft_adjust_request$
DECLARE p one_periods%ROWTYPE;
 minor smallint;
BEGIN
 IF NOT one_actor_has_permission(
   p_tenant,p_actor,'finance.period.soft_adjust') OR
   p_request IS NULL OR p_journal IS NULL OR p_request=p_journal OR
   p_on IS NULL OR p_on>current_date OR
   p_amount IS NULL OR p_amount<=0 OR p_currency IS NULL OR
   p_debit IS NULL OR p_credit IS NULL OR p_debit=p_credit OR
   p_reason IS NULL OR length(btrim(p_reason)) NOT BETWEEN 10 AND 500 THEN
  RAISE EXCEPTION 'Permission and valid two-sided adjustment evidence required';
 END IF;
 SELECT * INTO p FROM one_periods WHERE tenant_id=p_tenant
   AND id=p_period FOR UPDATE;
 IF NOT FOUND OR p.closed OR p_on<p.begins OR p_on>=p.ends_exclusive OR
   NOT EXISTS(SELECT 1 FROM one_period_soft_close_approvals
      WHERE tenant_id=p_tenant AND period_id=p_period) THEN
  RAISE EXCEPTION 'Only a reviewed soft-closed period accepts late adjustments';
 END IF;
 SELECT minor_units INTO minor FROM one_currency_precision WHERE code=p_currency;
 IF minor IS NULL OR p_amount<>round(p_amount,minor) OR
   NOT EXISTS(SELECT 1 FROM one_accounts a WHERE a.tenant_id=p_tenant
     AND a.id=p_debit AND a.active AND a.posting_allowed) OR
   NOT EXISTS(SELECT 1 FROM one_accounts a WHERE a.tenant_id=p_tenant
     AND a.id=p_credit AND a.active AND a.posting_allowed) THEN
  RAISE EXCEPTION 'Adjustment currency, accounts or minor units invalid';
 END IF;
 PERFORM set_config('one.soft_journal_building','yes',true);
 INSERT INTO one_journal_entries(
  tenant_id,id,period_id,reference,idempotency_key,occurred_on,currency)
 VALUES(p_tenant,p_journal,p_period,
    'Reviewed soft adjustment '||p_request::text,
    'soft-adjust:'||p_request::text,p_on,p_currency);
 INSERT INTO one_journal_lines(
  tenant_id,entry_id,line_number,account_id,debit,credit)
 VALUES(p_tenant,p_journal,1,p_debit,p_amount,0),
       (p_tenant,p_journal,2,p_credit,0,p_amount);
 PERFORM set_config('one.soft_journal_building','',true);
 PERFORM set_config('one.soft_workflow_write','yes',true);
 INSERT INTO one_soft_adjustment_requests(
  tenant_id,id,period_id,journal_id,requested_by,reason)
 VALUES(p_tenant,p_request,p_period,p_journal,p_actor,btrim(p_reason));
 PERFORM set_config('one.soft_workflow_write','',true);
 RETURN p_request;
END $soft_adjust_request$;

CREATE FUNCTION one_approve_soft_adjustment(
 p_tenant uuid,p_request uuid,p_actor uuid
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $soft_adjust_approve$
DECLARE r one_soft_adjustment_requests%ROWTYPE;
 p one_periods%ROWTYPE;
 j one_journal_entries%ROWTYPE;
BEGIN
 IF NOT one_actor_has_permission(
    p_tenant,p_actor,'finance.period.soft_adjust.approve') THEN
  RAISE EXCEPTION 'User lacks permission to approve late-period adjustment';
 END IF;
 SELECT * INTO r FROM one_soft_adjustment_requests
  WHERE tenant_id=p_tenant AND id=p_request FOR UPDATE;
 IF NOT FOUND OR r.requested_by=p_actor OR NOT EXISTS(
  SELECT 1 FROM one_memberships m WHERE m.tenant_id=p_tenant
    AND m.actor_id=r.requested_by AND m.active) THEN
  RAISE EXCEPTION 'A distinct active adjustment requester is required';
 END IF;
 SELECT * INTO p FROM one_periods WHERE tenant_id=p_tenant
    AND id=r.period_id FOR UPDATE;
 SELECT * INTO j FROM one_journal_entries WHERE tenant_id=p_tenant
    AND id=r.journal_id FOR UPDATE;
 IF p.closed OR NOT FOUND OR j.status<>'draft' OR j.period_id<>r.period_id THEN
  RAISE EXCEPTION 'Only intact draft adjustment may be reviewed';
 END IF;
 PERFORM set_config('one.soft_workflow_write','yes',true);
 INSERT INTO one_soft_adjustment_approvals(tenant_id,request_id,approved_by)
 VALUES(p_tenant,p_request,p_actor);
 PERFORM set_config('one.soft_workflow_write','',true);
 RETURN p_request;
END $soft_adjust_approve$;

-- Supplement one_post_journal, without replacing its currency, account,
-- balanced-lines, member and period date checks.
CREATE FUNCTION one_soft_journal_guard()
 RETURNS trigger LANGUAGE plpgsql AS $soft_journal$
DECLARE
 changed_period uuid;
 r one_soft_adjustment_requests%ROWTYPE;
 actor uuid;
BEGIN
 actor=nullif(current_setting('one.actor_id',true),'')::uuid;
 IF TG_OP='INSERT' THEN
  -- Period SHARE lock serializes inserts with approval's period FOR UPDATE.
  PERFORM 1 FROM one_periods p WHERE p.tenant_id=NEW.tenant_id
   AND p.id=NEW.period_id FOR SHARE;
  IF EXISTS(SELECT 1 FROM one_period_soft_close_approvals a
    WHERE a.tenant_id=NEW.tenant_id AND a.period_id=NEW.period_id)
    AND (current_setting('one.soft_journal_building',true)
           IS DISTINCT FROM 'yes' OR
         NOT one_actor_has_permission(
          NEW.tenant_id,actor,'finance.period.soft_adjust')) THEN
   RAISE EXCEPTION 'Ordinary journal inserts are frozen in soft-closed period';
  END IF;
  RETURN NEW;
 ELSIF TG_OP='DELETE' THEN
  IF EXISTS(SELECT 1 FROM one_soft_adjustment_requests r
     WHERE r.tenant_id=OLD.tenant_id AND r.journal_id=OLD.id) THEN
   RAISE EXCEPTION 'Submitted adjustment journal cannot be deleted';
  END IF;
  RETURN OLD;
 END IF;

 -- Never edit the immutable journal basis after soft adjustment submitted,
 -- nor move unrelated drafts into/out of a soft-closed period.
 IF EXISTS(SELECT 1 FROM one_period_soft_close_approvals p
   WHERE p.tenant_id=NEW.tenant_id AND p.period_id IN (
     OLD.period_id,NEW.period_id)) THEN
  SELECT * INTO r FROM one_soft_adjustment_requests x
   WHERE x.tenant_id=NEW.tenant_id AND x.journal_id=NEW.id;
  IF NOT FOUND THEN
   RAISE EXCEPTION 'Soft-closed period rejects unapproved journal changes';
  END IF;
  IF NEW.period_id IS DISTINCT FROM OLD.period_id OR
      NEW.occurred_on IS DISTINCT FROM OLD.occurred_on OR
      NEW.currency IS DISTINCT FROM OLD.currency OR
      NEW.reference IS DISTINCT FROM OLD.reference OR
      NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key OR
      NEW.status IS DISTINCT FROM 'posted' OR
      OLD.status IS DISTINCT FROM 'draft' OR
      NEW.posted_by IS DISTINCT FROM r.requested_by OR
      current_setting('one.posting_context',true) IS DISTINCT FROM 'yes' OR
      actor IS DISTINCT FROM r.requested_by OR
      NOT one_actor_has_permission(
        NEW.tenant_id,actor,'finance.period.soft_adjust') OR
      NOT EXISTS(SELECT 1 FROM one_soft_adjustment_approvals a
       WHERE a.tenant_id=r.tenant_id AND a.request_id=r.id) THEN
   RAISE EXCEPTION 'Only the authorized requester can post an independently approved, unchanged soft adjustment';
  END IF;
  PERFORM set_config('one.soft_workflow_write','yes',true);
  INSERT INTO one_soft_adjustment_post_audit(
     tenant_id,request_id,journal_id,posted_by)
  VALUES(r.tenant_id,r.id,r.journal_id,actor);
  PERFORM set_config('one.soft_workflow_write','',true);
 END IF;
 RETURN NEW;
END $soft_journal$;
CREATE TRIGGER one_soft_journal_enforce
 BEFORE INSERT OR UPDATE OR DELETE ON one_journal_entries
 FOR EACH ROW EXECUTE FUNCTION one_soft_journal_guard();

CREATE FUNCTION one_soft_adjustment_line_guard()
 RETURNS trigger LANGUAGE plpgsql AS $soft_line$
DECLARE t uuid; j uuid;
BEGIN
 IF TG_OP='DELETE' THEN
  t=OLD.tenant_id; j=OLD.entry_id;
 ELSE
  t=NEW.tenant_id; j=NEW.entry_id;
 END IF;
 IF EXISTS(SELECT 1 FROM one_soft_adjustment_requests r
  WHERE r.tenant_id=t AND r.journal_id=j) THEN
  RAISE EXCEPTION 'Reviewed soft-adjustment journal lines are immutable';
 END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END $soft_line$;
CREATE TRIGGER one_soft_adjustment_lines_frozen
 BEFORE INSERT OR UPDATE OR DELETE ON one_journal_lines
 FOR EACH ROW EXECUTE FUNCTION one_soft_adjustment_line_guard();

-- A direct period close is not mistaken for journal soft reopening;
-- existing 048 hard-close permissions and 045/047 close gates are retained.
CREATE FUNCTION one_finance_period_state(
 p_tenant uuid,p_period uuid,p_actor uuid
) RETURNS TABLE(period_state text,can_request_adjustment boolean,
 pending_adjustments bigint,approved_adjustments bigint,
 posted_adjustments bigint)
 LANGUAGE plpgsql STABLE SECURITY INVOKER AS $period_view$
DECLARE p one_periods%ROWTYPE;
BEGIN
 IF NOT one_actor_has_permission(
    p_tenant,p_actor,'finance.period.soft_close.view') THEN
  RAISE EXCEPTION 'User cannot read period-close controls';
 END IF;
 SELECT * INTO p FROM one_periods
  WHERE tenant_id=p_tenant AND id=p_period;
 IF NOT FOUND THEN RAISE EXCEPTION 'Accounting period missing'; END IF;
 RETURN QUERY SELECT
  (CASE WHEN p.closed THEN 'hard_closed'
        WHEN EXISTS(SELECT 1 FROM one_period_soft_close_approvals a
          WHERE a.tenant_id=p_tenant AND a.period_id=p_period) THEN 'soft_closed'
        ELSE 'open' END)::text,
  (NOT p.closed AND EXISTS(
    SELECT 1 FROM one_period_soft_close_approvals a
    WHERE a.tenant_id=p_tenant AND a.period_id=p_period)
    AND one_actor_has_permission(
      p_tenant,p_actor,'finance.period.soft_adjust'))::boolean,
  (SELECT count(*)::bigint FROM one_soft_adjustment_requests r
     WHERE r.tenant_id=p_tenant AND r.period_id=p_period
       AND NOT EXISTS(SELECT 1 FROM one_soft_adjustment_approvals a
         WHERE a.tenant_id=r.tenant_id AND a.request_id=r.id)),
  (SELECT count(*)::bigint FROM one_soft_adjustment_approvals a
   JOIN one_soft_adjustment_requests r ON r.tenant_id=a.tenant_id
      AND r.id=a.request_id
   WHERE r.tenant_id=p_tenant AND r.period_id=p_period),
  (SELECT count(*)::bigint FROM one_soft_adjustment_post_audit a
   JOIN one_soft_adjustment_requests r ON r.tenant_id=a.tenant_id
      AND r.id=a.request_id
   WHERE r.tenant_id=p_tenant AND r.period_id=p_period);
END $period_view$;

REVOKE ALL ON one_period_soft_close_requests,
 one_period_soft_close_approvals,one_soft_adjustment_requests,
 one_soft_adjustment_approvals,one_soft_adjustment_post_audit FROM PUBLIC;
REVOKE ALL ON FUNCTION one_request_period_soft_close(uuid,uuid,uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION one_approve_period_soft_close(uuid,uuid,uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION one_request_soft_adjustment(
 uuid,uuid,uuid,uuid,date,text,text,text,numeric,text,uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION one_approve_soft_adjustment(uuid,uuid,uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION one_finance_period_state(uuid,uuid,uuid) FROM PUBLIC;
COMMIT;
