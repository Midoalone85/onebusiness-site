-- ONE ERP NEXT / 051 | Real posted-GL comparison, independent approval
-- snapshot, and review-to-current drift for soft-period adjustments.
-- PostgreSQL 14 disposable pilot only. This is an accounting-period, single-
-- currency ledger presentation, NOT statutory statements or IFRS sign-off.
-- Never present a DRAFT what-if as posted actuals; do not convert currency.
-- Untrusted client-controlled session GUC values are NOT authentication.
BEGIN;

INSERT INTO one_permission_templates(permission_key,role,default_allow,risk_class)
VALUES
 ('finance.period.statement.preview','company_admin',true,'finance'),
 ('finance.period.statement.preview','accountant',true,'finance'),
 ('finance.period.statement.preview','viewer',false,'finance');
INSERT INTO one_permission_keys(permission_key)
 VALUES('finance.period.statement.preview');

-- Uses only posted journals from the SAME tenant, accounting period,
-- and currency. All seven sections keep the GL debit/credit sign conventions.
-- A candidate posted journal is excluded once from the 'before' baseline
-- and added back in 'after', avoiding double-counted posting. 'Before'
-- means the *currently observable* posted ledger excluding this candidate:
-- it is not a timestamp-frozen state until an approval seal is created.
CREATE FUNCTION one_soft_adjustment_statement_comparison(
 p_tenant uuid,p_request uuid,p_actor uuid
) RETURNS TABLE(
 section text,currency text,period_id uuid,
 before_posted numeric,adjustment_effect numeric,after_if_applied numeric,
 journal_status text,report_basis text
) LANGUAGE plpgsql STABLE SECURITY INVOKER AS $comparison$
DECLARE req one_soft_adjustment_requests%ROWTYPE;
 j one_journal_entries%ROWTYPE;
BEGIN
 IF p_tenant IS NULL OR p_request IS NULL OR p_actor IS NULL OR
    NOT one_actor_has_permission(
      p_tenant,p_actor,'finance.period.statement.preview') THEN
  RAISE EXCEPTION 'Financial preview permission required for tenant actor';
 END IF;
 SELECT * INTO req FROM one_soft_adjustment_requests r
   WHERE r.tenant_id=p_tenant AND r.id=p_request;
 IF NOT FOUND OR (req.requested_by<>p_actor AND
    NOT one_actor_has_permission(
      p_tenant,p_actor,'finance.period.soft_adjust.approve')) THEN
  RAISE EXCEPTION 'Only requester or permitted adjustment reviewer can compare';
 END IF;
 SELECT * INTO j FROM one_journal_entries e
   WHERE e.tenant_id=p_tenant AND e.id=req.journal_id
     AND e.period_id=req.period_id;
 IF NOT FOUND OR j.status NOT IN ('draft','posted') THEN
  RAISE EXCEPTION 'Adjustment journal missing or outside requested period';
 END IF;

 RETURN QUERY
 WITH baseline AS (
   SELECT
    coalesce(sum(CASE WHEN a.kind='asset'
      THEN l.debit-l.credit ELSE 0 END),0)::numeric AS assets,
    coalesce(sum(CASE WHEN a.kind='liability'
      THEN l.credit-l.debit ELSE 0 END),0)::numeric AS liabilities,
    coalesce(sum(CASE WHEN a.kind='equity'
      THEN l.credit-l.debit ELSE 0 END),0)::numeric AS equity,
    coalesce(sum(CASE WHEN a.kind='revenue'
      THEN l.credit-l.debit ELSE 0 END),0)::numeric AS revenue,
    coalesce(sum(CASE WHEN a.kind='expense'
      THEN l.debit-l.credit ELSE 0 END),0)::numeric AS expenses
   FROM one_journal_entries e
   JOIN one_journal_lines l ON l.tenant_id=e.tenant_id
      AND l.entry_id=e.id
   JOIN one_accounts a ON a.tenant_id=l.tenant_id AND a.id=l.account_id
   WHERE e.tenant_id=p_tenant AND e.period_id=j.period_id
     AND e.currency=j.currency AND e.status='posted'
     AND e.id<>j.id
 ), proposed AS (
   SELECT
    coalesce(sum(CASE WHEN a.kind='asset'
      THEN l.debit-l.credit ELSE 0 END),0)::numeric AS assets,
    coalesce(sum(CASE WHEN a.kind='liability'
      THEN l.credit-l.debit ELSE 0 END),0)::numeric AS liabilities,
    coalesce(sum(CASE WHEN a.kind='equity'
      THEN l.credit-l.debit ELSE 0 END),0)::numeric AS equity,
    coalesce(sum(CASE WHEN a.kind='revenue'
      THEN l.credit-l.debit ELSE 0 END),0)::numeric AS revenue,
    coalesce(sum(CASE WHEN a.kind='expense'
      THEN l.debit-l.credit ELSE 0 END),0)::numeric AS expenses
   FROM one_journal_lines l
   JOIN one_accounts a ON a.tenant_id=l.tenant_id AND a.id=l.account_id
   WHERE l.tenant_id=p_tenant AND l.entry_id=j.id
 ), amounts AS (
   SELECT v.metric,v.base,v.effect
   FROM baseline b CROSS JOIN proposed d
   CROSS JOIN LATERAL (VALUES
      ('assets',b.assets,d.assets),
      ('liabilities',b.liabilities,d.liabilities),
      ('equity',b.equity,d.equity),
      ('revenue',b.revenue,d.revenue),
      ('expenses',b.expenses,d.expenses),
      ('net_income',b.revenue-b.expenses,d.revenue-d.expenses),
      ('accounting_equation_gap',
       b.assets-b.liabilities-b.equity-(b.revenue-b.expenses),
       d.assets-d.liabilities-d.equity-(d.revenue-d.expenses))
   ) AS v(metric,base,effect)
 )
 SELECT v.metric::text,j.currency::text,j.period_id,
   v.base::numeric,v.effect::numeric,(v.base+v.effect)::numeric,
   j.status::text,
   (CASE WHEN j.status='draft' THEN
     'LIVE_PERIOD_POSTED_GL_WITH_DRAFT_WHAT_IF'
    ELSE 'CURRENT_PERIOD_POSTED_GL_EXCLUDING_THIS_POSTED_ADJUSTMENT'
    END)::text
 FROM amounts v ORDER BY v.metric;
END $comparison$;

-- Every approval after #051 atomically persists its original baseline,
-- proposed delta and projected statement. Existing #050 approvals are NOT
-- backfilled, since current ledger would be a false historical approximation.
CREATE TABLE one_soft_adjustment_statement_approval_seals(
 tenant_id uuid NOT NULL,
 request_id uuid NOT NULL,
 section text NOT NULL
   CHECK(section IN('assets','liabilities','equity','revenue',
     'expenses','net_income','accounting_equation_gap')),
 period_id uuid NOT NULL,
 currency text NOT NULL REFERENCES one_currency_precision(code),
 approved_by uuid NOT NULL,
 before_at_approval numeric(24,3) NOT NULL,
 effect_at_approval numeric(24,3) NOT NULL,
 projected_at_approval numeric(24,3) NOT NULL,
 journal_fingerprint text NOT NULL CHECK(length(journal_fingerprint)=32),
 snapshot_basis text NOT NULL
  DEFAULT 'APPROVAL_TIME_POSTED_GL_PLUS_UNPOSTED_ADJUSTMENT'
  CHECK(snapshot_basis='APPROVAL_TIME_POSTED_GL_PLUS_UNPOSTED_ADJUSTMENT'),
 sealed_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 PRIMARY KEY(tenant_id,request_id,section),
 FOREIGN KEY(tenant_id,request_id)
  REFERENCES one_soft_adjustment_approvals(tenant_id,request_id),
 FOREIGN KEY(tenant_id,period_id)
  REFERENCES one_periods(tenant_id,id),
 CHECK(projected_at_approval=before_at_approval+effect_at_approval)
);
ALTER TABLE one_soft_adjustment_statement_approval_seals ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_soft_adjustment_statement_approval_seals FORCE ROW LEVEL SECURITY;
CREATE POLICY one_soft_statement_approval_scope
 ON one_soft_adjustment_statement_approval_seals
 USING(tenant_id::text=current_setting('one.tenant_id',true))
 WITH CHECK(tenant_id::text=current_setting('one.tenant_id',true));

CREATE FUNCTION one_soft_statement_seal_write_guard()
 RETURNS trigger LANGUAGE plpgsql AS $seal_guard$
BEGIN
 IF TG_OP<>'INSERT' OR current_setting(
   'one.soft_statement_sealing',true) IS DISTINCT FROM 'yes' THEN
  RAISE EXCEPTION 'Approved period financial snapshots are immutable audit';
 END IF;
 RETURN NEW;
END $seal_guard$;
CREATE TRIGGER one_soft_statement_seals_append_only
 BEFORE INSERT OR UPDATE OR DELETE ON one_soft_adjustment_statement_approval_seals
 FOR EACH ROW EXECUTE FUNCTION one_soft_statement_seal_write_guard();

CREATE FUNCTION one_capture_soft_adjustment_statement_approval()
 RETURNS trigger LANGUAGE plpgsql AS $capture_statement$
DECLARE r one_soft_adjustment_requests%ROWTYPE;
 evidence one_soft_adjustment_approval_evidence%ROWTYPE;
 captured bigint;
BEGIN
 SELECT * INTO r FROM one_soft_adjustment_requests
 WHERE tenant_id=NEW.tenant_id AND id=NEW.request_id;
 SELECT * INTO evidence FROM one_soft_adjustment_approval_evidence
 WHERE tenant_id=NEW.tenant_id AND request_id=NEW.request_id;
 IF NOT FOUND OR evidence.journal_id IS DISTINCT FROM r.journal_id THEN
  RAISE EXCEPTION 'Financial approval requires immutable GL source evidence';
 END IF;
 PERFORM set_config('one.soft_statement_sealing','yes',true);
 INSERT INTO one_soft_adjustment_statement_approval_seals(
  tenant_id,request_id,section,period_id,currency,approved_by,
  before_at_approval,effect_at_approval,projected_at_approval,
  journal_fingerprint)
 SELECT NEW.tenant_id,NEW.request_id,s.section,s.period_id,s.currency,
   NEW.approved_by,s.before_posted,s.adjustment_effect,
   s.after_if_applied,evidence.ledger_fingerprint
 FROM one_soft_adjustment_statement_comparison(
   NEW.tenant_id,NEW.request_id,NEW.approved_by) s
 WHERE s.journal_status='draft'
   AND s.report_basis='LIVE_PERIOD_POSTED_GL_WITH_DRAFT_WHAT_IF';
 GET DIAGNOSTICS captured=ROW_COUNT;
 IF captured<>7 THEN
  RAISE EXCEPTION 'Incomplete financial approval snapshot';
 END IF;
 PERFORM set_config('one.soft_statement_sealing','',true);
 RETURN NEW;
END $capture_statement$;
-- PostgreSQL runs same-timing triggers in alphabetical name order: the
-- existing 050 "one_soft_adjust_evidence_on_approval" runs first.
CREATE TRIGGER one_soft_statement_on_approval
 AFTER INSERT ON one_soft_adjustment_approvals
 FOR EACH ROW EXECUTE FUNCTION one_capture_soft_adjustment_statement_approval();

-- Live read comparing original approval snapshot vs current posted GL.
-- A later posted journal can cause a different baseline; the drift is
-- explicit rather than quietly replacing the approved values.
CREATE FUNCTION one_soft_adjustment_statement_drift(
 p_tenant uuid,p_request uuid,p_actor uuid
) RETURNS TABLE(
 section text,currency text,period_id uuid,
 baseline_when_approved numeric,approved_effect numeric,
 forecast_when_approved numeric,current_baseline numeric,
 current_effect numeric,current_forecast numeric,
 baseline_drift numeric,journal_status text,approved_at timestamptz
) LANGUAGE plpgsql STABLE SECURITY INVOKER AS $statement_drift$
DECLARE matches bigint;
BEGIN
 -- The underlying 051 comparison authenticates the actor + document access.
 RETURN QUERY
 WITH live AS (
  SELECT * FROM one_soft_adjustment_statement_comparison(
    p_tenant,p_request,p_actor)
 ), paired AS (
  SELECT s.section,s.currency,s.period_id,
   s.before_at_approval,s.effect_at_approval,s.projected_at_approval,
   l.before_posted,l.adjustment_effect,l.after_if_applied,
   (l.before_posted-s.before_at_approval) AS drift,
   l.journal_status,s.sealed_at
  FROM one_soft_adjustment_statement_approval_seals s
  JOIN live l ON l.section=s.section AND l.currency=s.currency
   AND l.period_id=s.period_id
  WHERE s.tenant_id=p_tenant AND s.request_id=p_request
 )
 SELECT p.section,p.currency,p.period_id,
   p.before_at_approval,p.effect_at_approval,p.projected_at_approval,
   p.before_posted,p.adjustment_effect,p.after_if_applied,
   p.drift,p.journal_status,p.sealed_at
 FROM paired p ORDER BY p.section;
 GET DIAGNOSTICS matches=ROW_COUNT;
 IF matches<>7 THEN
  RAISE EXCEPTION 'Approval-time financial comparison not available';
 END IF;
END $statement_drift$;

REVOKE ALL ON one_soft_adjustment_statement_approval_seals FROM PUBLIC;
REVOKE ALL ON FUNCTION one_soft_adjustment_statement_comparison(
 uuid,uuid,uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION one_soft_adjustment_statement_drift(
 uuid,uuid,uuid) FROM PUBLIC;
COMMIT;
