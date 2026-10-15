-- ONE ERP NEXT / 050 | Multi-line soft-period adjustments, per-actor limits,
-- immutable approval evidence and read-only account impact preview.
-- Independent PostgreSQL 14 pilot; no invoicing/stock/FX workflow implied.
-- Only SAR has conservative seeded role limits; other currencies fail closed
-- until their own reviewed actor limit is approved (never compare FX blindly).
-- SQL session GUCs are backend identity inputs, NOT user authentication.
BEGIN;

CREATE TABLE one_soft_adjust_default_limits(
 role text NOT NULL CHECK(role IN ('company_admin','accountant','viewer')),
 currency text NOT NULL REFERENCES one_currency_precision(code),
 max_debit numeric(20,3) NOT NULL CHECK(max_debit>=0),
 PRIMARY KEY(role,currency)
);
INSERT INTO one_soft_adjust_default_limits(role,currency,max_debit) VALUES
 ('company_admin','SAR',100000),
 ('accountant','SAR',25000),
 ('viewer','SAR',0);

CREATE TABLE one_soft_adjust_limit_requests(
 tenant_id uuid NOT NULL REFERENCES one_tenants(id),
 id uuid NOT NULL,
 target_actor_id uuid NOT NULL,
 currency text NOT NULL REFERENCES one_currency_precision(code),
 max_debit numeric(20,3) NOT NULL CHECK(max_debit>=0),
 effective_from date NOT NULL,
 effective_until date,
 requested_by uuid NOT NULL,
 requested_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 PRIMARY KEY(tenant_id,id),
 FOREIGN KEY(tenant_id,target_actor_id)
   REFERENCES one_memberships(tenant_id,actor_id),
 CHECK(effective_until IS NULL OR effective_until>effective_from)
);
CREATE TABLE one_soft_adjust_limit_approvals(
 tenant_id uuid NOT NULL,
 request_id uuid NOT NULL,
 approved_by uuid NOT NULL,
 approved_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 sequence_no bigint GENERATED ALWAYS AS IDENTITY,
 PRIMARY KEY(tenant_id,request_id),
 FOREIGN KEY(tenant_id,request_id)
  REFERENCES one_soft_adjust_limit_requests(tenant_id,id)
);

ALTER TABLE one_soft_adjust_limit_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_soft_adjust_limit_requests FORCE ROW LEVEL SECURITY;
CREATE POLICY one_soft_adjust_limit_request_scope
 ON one_soft_adjust_limit_requests
 USING(tenant_id::text=current_setting('one.tenant_id',true))
 WITH CHECK(tenant_id::text=current_setting('one.tenant_id',true));
ALTER TABLE one_soft_adjust_limit_approvals ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_soft_adjust_limit_approvals FORCE ROW LEVEL SECURITY;
CREATE POLICY one_soft_adjust_limit_approval_scope
 ON one_soft_adjust_limit_approvals
 USING(tenant_id::text=current_setting('one.tenant_id',true))
 WITH CHECK(tenant_id::text=current_setting('one.tenant_id',true));

CREATE FUNCTION one_soft_adjust_limit_fact_guard() RETURNS trigger
 LANGUAGE plpgsql AS $limit_fact$
BEGIN
 IF TG_OP<>'INSERT' OR
    current_setting('one.soft_limit_workflow',true) IS DISTINCT FROM 'yes' THEN
  RAISE EXCEPTION 'Soft adjustment amount permissions require reviewed workflow';
 END IF;
 RETURN NEW;
END $limit_fact$;
CREATE TRIGGER one_soft_limit_request_append_only
 BEFORE INSERT OR UPDATE OR DELETE ON one_soft_adjust_limit_requests
 FOR EACH ROW EXECUTE FUNCTION one_soft_adjust_limit_fact_guard();
CREATE TRIGGER one_soft_limit_approval_append_only
 BEFORE INSERT OR UPDATE OR DELETE ON one_soft_adjust_limit_approvals
 FOR EACH ROW EXECUTE FUNCTION one_soft_adjust_limit_fact_guard();

-- Return NULL for missing limit instead of defaulting to unlimited.
-- Permission and tenant/session identity must also be valid.
CREATE FUNCTION one_actor_soft_adjust_limit(
 p_tenant uuid,p_actor uuid,p_currency text
) RETURNS numeric LANGUAGE plpgsql STABLE SECURITY INVOKER AS $get_limit$
DECLARE r text;
 limit_value numeric;
BEGIN
 IF p_currency IS NULL OR NOT one_actor_has_permission(
  p_tenant,p_actor,'finance.period.soft_adjust') THEN
  RETURN NULL;
 END IF;
 SELECT m.role INTO r FROM one_memberships m WHERE
  m.tenant_id=p_tenant AND m.actor_id=p_actor AND m.active;
 IF r IS NULL THEN RETURN NULL; END IF;
 SELECT q.max_debit INTO limit_value
 FROM one_soft_adjust_limit_requests q
 JOIN one_soft_adjust_limit_approvals a
   ON a.tenant_id=q.tenant_id AND a.request_id=q.id
 WHERE q.tenant_id=p_tenant AND q.target_actor_id=p_actor
   AND q.currency=p_currency AND q.effective_from<=current_date
   AND (q.effective_until IS NULL OR q.effective_until>current_date)
 ORDER BY a.sequence_no DESC LIMIT 1;
 IF FOUND THEN RETURN limit_value; END IF;
 SELECT d.max_debit INTO limit_value FROM one_soft_adjust_default_limits d
 WHERE d.role=r AND d.currency=p_currency;
 RETURN limit_value;
END $get_limit$;

CREATE FUNCTION one_request_soft_adjust_limit(
 p_tenant uuid,p_request uuid,p_target uuid,p_currency text,
 p_limit numeric,p_from date,p_until date,p_actor uuid
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $limit_request$
DECLARE precision integer;
BEGIN
 IF p_request IS NULL OR p_target IS NULL OR p_from IS NULL OR
    p_limit IS NULL OR p_limit<0 OR p_currency IS NULL OR
    (p_until IS NOT NULL AND p_until<=p_from) OR
    NOT one_actor_has_permission(
      p_tenant,p_actor,'finance.permission.change.request') OR
    NOT EXISTS(SELECT 1 FROM one_memberships m WHERE
      m.tenant_id=p_tenant AND m.actor_id=p_actor AND m.active
      AND m.role='company_admin') OR
    NOT EXISTS(SELECT 1 FROM one_memberships m WHERE
      m.tenant_id=p_tenant AND m.actor_id=p_target AND m.active) THEN
  RAISE EXCEPTION 'Authorized manager, active target and valid amount cap required';
 END IF;
 SELECT minor_units INTO precision FROM one_currency_precision
 WHERE code=p_currency;
 IF precision IS NULL OR p_limit<>round(p_limit,precision) THEN
  RAISE EXCEPTION 'Amount permission currency precision invalid';
 END IF;
 PERFORM set_config('one.soft_limit_workflow','yes',true);
 INSERT INTO one_soft_adjust_limit_requests(
  tenant_id,id,target_actor_id,currency,max_debit,effective_from,
  effective_until,requested_by)
 VALUES(p_tenant,p_request,p_target,p_currency,p_limit,
    p_from,p_until,p_actor);
 PERFORM set_config('one.soft_limit_workflow','',true);
 RETURN p_request;
END $limit_request$;

CREATE FUNCTION one_approve_soft_adjust_limit(
 p_tenant uuid,p_request uuid,p_actor uuid
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $limit_approve$
DECLARE request one_soft_adjust_limit_requests%ROWTYPE;
BEGIN
 IF NOT one_actor_has_permission(
    p_tenant,p_actor,'finance.permission.change.approve') OR
    NOT EXISTS(SELECT 1 FROM one_memberships m WHERE
      m.tenant_id=p_tenant AND m.actor_id=p_actor AND m.active
      AND m.role IN ('company_admin','accountant')) THEN
  RAISE EXCEPTION 'Independent permission reviewer required';
 END IF;
 SELECT * INTO request FROM one_soft_adjust_limit_requests
 WHERE tenant_id=p_tenant AND id=p_request FOR UPDATE;
 IF NOT FOUND OR request.requested_by=p_actor OR NOT EXISTS(
   SELECT 1 FROM one_memberships m WHERE m.tenant_id=p_tenant
    AND m.actor_id=request.requested_by AND m.active
    AND m.role='company_admin') THEN
  RAISE EXCEPTION 'Distinct active manager must request the limit';
 END IF;
 PERFORM set_config('one.soft_limit_workflow','yes',true);
 INSERT INTO one_soft_adjust_limit_approvals(
   tenant_id,request_id,approved_by)
 VALUES(p_tenant,p_request,p_actor);
 PERFORM set_config('one.soft_limit_workflow','',true);
 RETURN p_request;
END $limit_approve$;

-- Use the SAME cap for original 049 two-line and newer 050 multi-line paths.
-- Enforce at creation, independent approval and posting; policy revocations
-- or newly lowered amount caps cannot be bypassed by an old approved draft.
CREATE FUNCTION one_soft_adjust_amount_gate() RETURNS trigger
 LANGUAGE plpgsql AS $amount_gate$
DECLARE r one_soft_adjustment_requests%ROWTYPE;
 j one_journal_entries%ROWTYPE;
 amount numeric;
 actor uuid;
 allowed numeric;
BEGIN
 IF TG_TABLE_NAME='one_soft_adjustment_requests' THEN
  r=NEW;
  actor=NEW.requested_by;
 ELSIF TG_TABLE_NAME='one_soft_adjustment_approvals' THEN
  SELECT * INTO r FROM one_soft_adjustment_requests
   WHERE tenant_id=NEW.tenant_id AND id=NEW.request_id;
  actor=NEW.approved_by;
 ELSE
  SELECT * INTO r FROM one_soft_adjustment_requests
   WHERE tenant_id=NEW.tenant_id AND journal_id=NEW.id;
  IF NOT FOUND THEN RETURN NEW; END IF;
  actor=NEW.posted_by;
 END IF;
 IF r.id IS NULL THEN RAISE EXCEPTION 'Soft adjustment request missing'; END IF;
 SELECT * INTO j FROM one_journal_entries
  WHERE tenant_id=r.tenant_id AND id=r.journal_id;
 IF TG_TABLE_NAME='one_journal_entries' THEN
  j.currency=NEW.currency;
 END IF;
 SELECT coalesce(sum(debit),0)::numeric INTO amount
 FROM one_journal_lines
 WHERE tenant_id=r.tenant_id AND entry_id=r.journal_id;
 allowed=one_actor_soft_adjust_limit(r.tenant_id,actor,j.currency);
 IF allowed IS NULL OR amount<=0 OR amount>allowed THEN
  RAISE EXCEPTION 'Soft adjustment debit total exceeds the current actor currency limit';
 END IF;
 RETURN NEW;
END $amount_gate$;
CREATE TRIGGER one_soft_adjust_limit_at_request
 BEFORE INSERT ON one_soft_adjustment_requests
 FOR EACH ROW EXECUTE FUNCTION one_soft_adjust_amount_gate();
CREATE TRIGGER one_soft_adjust_limit_at_approval
 BEFORE INSERT ON one_soft_adjustment_approvals
 FOR EACH ROW EXECUTE FUNCTION one_soft_adjust_amount_gate();
CREATE TRIGGER one_soft_adjust_limit_at_post
 BEFORE UPDATE OF status ON one_journal_entries
 FOR EACH ROW WHEN (NEW.status='posted' AND OLD.status='draft')
 EXECUTE FUNCTION one_soft_adjust_amount_gate();

-- Multi-line entries are created by a single trusted service call; JSONB is
-- a bounded transport only. No user-provided SQL, no missing line types.
CREATE FUNCTION one_request_soft_adjustment_lines(
 p_tenant uuid,p_request uuid,p_period uuid,p_journal uuid,p_on date,
 p_currency text,p_lines jsonb,p_reason text,p_actor uuid
) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $multi_adjust$
DECLARE p one_periods%ROWTYPE;
 minor smallint;
 row_json jsonb;
 account text;
 debit numeric;
 credit numeric;
 sum_debit numeric:=0;
 sum_credit numeric:=0;
 n integer:=0;
BEGIN
 IF NOT one_actor_has_permission(
  p_tenant,p_actor,'finance.period.soft_adjust') OR
  p_request IS NULL OR p_journal IS NULL OR p_request=p_journal OR
  p_on IS NULL OR p_on>current_date OR
  p_currency IS NULL OR p_reason IS NULL OR
  length(btrim(p_reason)) NOT BETWEEN 10 AND 500 OR
  p_lines IS NULL OR jsonb_typeof(p_lines)<>'array' OR
  jsonb_array_length(p_lines) NOT BETWEEN 2 AND 25 THEN
  RAISE EXCEPTION 'Valid approved-period multi-line adjustment required';
 END IF;
 SELECT * INTO p FROM one_periods
  WHERE tenant_id=p_tenant AND id=p_period FOR UPDATE;
 IF NOT FOUND OR p.closed OR p_on<p.begins OR p_on>=p.ends_exclusive OR
   NOT EXISTS(SELECT 1 FROM one_period_soft_close_approvals
     WHERE tenant_id=p_tenant AND period_id=p_period) THEN
  RAISE EXCEPTION 'Period must be independently soft-closed before adjustment';
 END IF;
 SELECT minor_units INTO minor FROM one_currency_precision
 WHERE code=p_currency;
 IF minor IS NULL THEN
  RAISE EXCEPTION 'Unrecognized journal currency';
 END IF;
 -- Validate the ENTIRE payload before persisting any journal rows.
 FOR row_json IN SELECT value FROM jsonb_array_elements(p_lines)
 LOOP
  IF jsonb_typeof(row_json)<>'object' OR
    jsonb_typeof(row_json->'account_id')<>'string' OR
    jsonb_typeof(row_json->'debit')<>'number' OR
    jsonb_typeof(row_json->'credit')<>'number' OR
    (SELECT count(*) FROM jsonb_object_keys(row_json))<>3 THEN
   RAISE EXCEPTION 'Each journal line requires only account_id, numeric debit and credit';
  END IF;
  account=row_json->>'account_id';
  debit=(row_json->>'debit')::numeric;
  credit=(row_json->>'credit')::numeric;
  IF (debit>0 AND credit=0) IS NOT TRUE AND
     (credit>0 AND debit=0) IS NOT TRUE THEN
    RAISE EXCEPTION 'Every journal line needs exactly one positive side';
  END IF;
  IF debit<>round(debit,minor) OR credit<>round(credit,minor) OR
    NOT EXISTS(SELECT 1 FROM one_accounts a WHERE
      a.tenant_id=p_tenant AND a.id=account AND a.active
      AND a.posting_allowed) THEN
   RAISE EXCEPTION 'Invalid posting account or currency decimal precision';
  END IF;
  sum_debit=sum_debit+debit;
  sum_credit=sum_credit+credit;
  n=n+1;
 END LOOP;
 IF sum_debit<=0 OR sum_debit<>sum_credit OR
    sum_debit>coalesce(one_actor_soft_adjust_limit(
      p_tenant,p_actor,p_currency),-1) THEN
  RAISE EXCEPTION 'Unbalanced multi-line journal or requester amount limit exceeded';
 END IF;

 PERFORM set_config('one.soft_journal_building','yes',true);
 INSERT INTO one_journal_entries(
  tenant_id,id,period_id,reference,idempotency_key,occurred_on,currency)
 VALUES(p_tenant,p_journal,p_period,
  'Reviewed soft adjustment '||p_request::text,
  'soft-adjust:'||p_request::text,p_on,p_currency);
 n=0;
 FOR row_json IN SELECT value FROM jsonb_array_elements(p_lines)
 LOOP
  n=n+1;
  INSERT INTO one_journal_lines(
   tenant_id,entry_id,line_number,account_id,debit,credit)
  VALUES(p_tenant,p_journal,n,row_json->>'account_id',
    (row_json->>'debit')::numeric,(row_json->>'credit')::numeric);
 END LOOP;
 PERFORM set_config('one.soft_journal_building','',true);
 PERFORM set_config('one.soft_workflow_write','yes',true);
 INSERT INTO one_soft_adjustment_requests(
  tenant_id,id,period_id,journal_id,requested_by,reason)
 VALUES(p_tenant,p_request,p_period,p_journal,p_actor,btrim(p_reason));
 PERFORM set_config('one.soft_workflow_write','',true);
 RETURN p_request;
END $multi_adjust$;

CREATE TABLE one_soft_adjustment_approval_evidence(
 tenant_id uuid NOT NULL,
 request_id uuid NOT NULL,
 journal_id uuid NOT NULL,
 currency text NOT NULL REFERENCES one_currency_precision(code),
 journal_lines integer NOT NULL CHECK(journal_lines BETWEEN 2 AND 25),
 total_debit numeric(20,3) NOT NULL CHECK(total_debit>0),
 total_credit numeric(20,3) NOT NULL CHECK(total_credit>0),
 ledger_fingerprint text NOT NULL CHECK(length(ledger_fingerprint)=32),
 recorded_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 PRIMARY KEY(tenant_id,request_id),
 FOREIGN KEY(tenant_id,request_id)
  REFERENCES one_soft_adjustment_approvals(tenant_id,request_id),
 FOREIGN KEY(tenant_id,journal_id)
  REFERENCES one_journal_entries(tenant_id,id),
 CHECK(total_debit=total_credit)
);
ALTER TABLE one_soft_adjustment_approval_evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_soft_adjustment_approval_evidence FORCE ROW LEVEL SECURITY;
CREATE POLICY one_soft_adjust_evidence_scope
 ON one_soft_adjustment_approval_evidence
 USING(tenant_id::text=current_setting('one.tenant_id',true))
 WITH CHECK(tenant_id::text=current_setting('one.tenant_id',true));
CREATE TRIGGER one_soft_adjust_evidence_immutable
 BEFORE UPDATE OR DELETE ON one_soft_adjustment_approval_evidence
 FOR EACH ROW EXECUTE FUNCTION one_protect_audit();

-- Snapshot values and digest at approval, inside the SAME transaction as
-- the 049 approval. Existing approved 049 journals remain compatible.
CREATE FUNCTION one_soft_adjustment_capture_evidence()
 RETURNS trigger LANGUAGE plpgsql AS $capture_evidence$
DECLARE req one_soft_adjustment_requests%ROWTYPE;
 j one_journal_entries%ROWTYPE;
 n integer;
 debit_amount numeric;
 credit_amount numeric;
 fingerprint text;
BEGIN
 SELECT * INTO req FROM one_soft_adjustment_requests
   WHERE tenant_id=NEW.tenant_id AND id=NEW.request_id;
 SELECT * INTO j FROM one_journal_entries
   WHERE tenant_id=NEW.tenant_id AND id=req.journal_id;
 SELECT count(*)::integer,coalesce(sum(debit),0),coalesce(sum(credit),0),
   md5(coalesce(string_agg(
     line_number::text||':'||account_id||':'||
     debit::text||':'||credit::text,'|' ORDER BY line_number),''))
 INTO n,debit_amount,credit_amount,fingerprint
 FROM one_journal_lines WHERE tenant_id=NEW.tenant_id AND entry_id=req.journal_id;
 IF n NOT BETWEEN 2 AND 25 OR debit_amount<=0 OR
    debit_amount<>credit_amount OR j.status<>'draft' THEN
  RAISE EXCEPTION 'Approval requires complete balanced immutable journal lines';
 END IF;
 INSERT INTO one_soft_adjustment_approval_evidence(
  tenant_id,request_id,journal_id,currency,journal_lines,total_debit,
  total_credit,ledger_fingerprint)
 VALUES(NEW.tenant_id,NEW.request_id,req.journal_id,j.currency,n,
        debit_amount,credit_amount,fingerprint);
 RETURN NEW;
END $capture_evidence$;
CREATE TRIGGER one_soft_adjust_evidence_on_approval
 AFTER INSERT ON one_soft_adjustment_approvals
 FOR EACH ROW EXECUTE FUNCTION one_soft_adjustment_capture_evidence();

-- A posted journal must still match its frozen approval evidence even if
-- a privileged editor bypassed a line mutation guard between steps.
CREATE FUNCTION one_soft_adjustment_evidence_post_gate()
 RETURNS trigger LANGUAGE plpgsql AS $evidence_at_post$
DECLARE req one_soft_adjustment_requests%ROWTYPE;
 prior one_soft_adjustment_approval_evidence%ROWTYPE;
 fingerprint text;
 n integer;
 debit_amount numeric;
 credit_amount numeric;
BEGIN
 SELECT * INTO req FROM one_soft_adjustment_requests
 WHERE tenant_id=NEW.tenant_id AND journal_id=NEW.id;
 IF NOT FOUND THEN RETURN NEW; END IF;
 SELECT * INTO prior FROM one_soft_adjustment_approval_evidence
 WHERE tenant_id=req.tenant_id AND request_id=req.id;
 SELECT count(*)::integer,coalesce(sum(debit),0),coalesce(sum(credit),0),
  md5(coalesce(string_agg(
    line_number::text||':'||account_id||':'||
    debit::text||':'||credit::text,'|' ORDER BY line_number),''))
 INTO n,debit_amount,credit_amount,fingerprint
 FROM one_journal_lines WHERE tenant_id=req.tenant_id AND entry_id=req.journal_id;
 IF prior.request_id IS NULL OR prior.journal_id<>NEW.id OR
   prior.currency<>NEW.currency OR prior.journal_lines<>n OR
   prior.total_debit<>debit_amount OR
   prior.total_credit<>credit_amount OR
   prior.ledger_fingerprint<>fingerprint THEN
  RAISE EXCEPTION 'Approval fingerprint no longer matches frozen GL adjustment';
 END IF;
 RETURN NEW;
END $evidence_at_post$;
CREATE TRIGGER one_soft_adjustment_evidence_at_post
 BEFORE UPDATE OF status ON one_journal_entries
 FOR EACH ROW WHEN (NEW.status='posted' AND OLD.status='draft')
 EXECUTE FUNCTION one_soft_adjustment_evidence_post_gate();

-- Preview must remain read-only and include account balances and net position
-- impact. This is an accounting delta, NOT automatic profit/revenue advice.
CREATE FUNCTION one_soft_adjustment_impact_preview(
 p_tenant uuid,p_request uuid,p_actor uuid
) RETURNS TABLE(
 account_id text,account_kind text,account_name text,
 currency text,total_debit numeric,total_credit numeric,
 net_debit_change numeric,approved boolean
) LANGUAGE plpgsql STABLE SECURITY INVOKER AS $impact$
DECLARE req one_soft_adjustment_requests%ROWTYPE;
 currency_code text;
BEGIN
 IF current_setting('one.tenant_id',true) IS DISTINCT FROM p_tenant::text OR
    current_setting('one.actor_id',true) IS DISTINCT FROM p_actor::text OR
    NOT EXISTS(SELECT 1 FROM one_memberships m WHERE
      m.tenant_id=p_tenant AND m.actor_id=p_actor AND m.active) THEN
  RAISE EXCEPTION 'A tenant-authenticated preview viewer is required';
 END IF;
 SELECT * INTO req FROM one_soft_adjustment_requests r
 WHERE r.tenant_id=p_tenant AND r.id=p_request;
 IF NOT FOUND OR (req.requested_by<>p_actor AND
    NOT one_actor_has_permission(
     p_tenant,p_actor,'finance.period.soft_adjust.approve')) THEN
  RAISE EXCEPTION 'Only requester or authorized reviewer may view journal impact';
 END IF;
 SELECT j.currency INTO currency_code FROM one_journal_entries j
  WHERE j.tenant_id=p_tenant AND j.id=req.journal_id;
 RETURN QUERY
 SELECT a.id,a.kind,a.name,currency_code,
   sum(l.debit)::numeric,sum(l.credit)::numeric,
   sum(l.debit-l.credit)::numeric,
   EXISTS(SELECT 1 FROM one_soft_adjustment_approvals q
    WHERE q.tenant_id=p_tenant AND q.request_id=p_request)
 FROM one_journal_lines l
 JOIN one_accounts a ON a.tenant_id=l.tenant_id AND a.id=l.account_id
 WHERE l.tenant_id=p_tenant AND l.entry_id=req.journal_id
 GROUP BY a.id,a.kind,a.name ORDER BY a.id;
END $impact$;

REVOKE ALL ON one_soft_adjust_default_limits,
 one_soft_adjust_limit_requests,one_soft_adjust_limit_approvals,
 one_soft_adjustment_approval_evidence FROM PUBLIC;
REVOKE ALL ON FUNCTION one_actor_soft_adjust_limit(
 uuid,uuid,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION one_request_soft_adjust_limit(
 uuid,uuid,uuid,text,numeric,date,date,uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION one_approve_soft_adjust_limit(
 uuid,uuid,uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION one_request_soft_adjustment_lines(
 uuid,uuid,uuid,uuid,date,text,jsonb,text,uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION one_soft_adjustment_impact_preview(
 uuid,uuid,uuid) FROM PUBLIC;
COMMIT;
