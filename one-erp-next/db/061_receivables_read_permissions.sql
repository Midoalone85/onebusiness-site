-- ONE ERP NEXT 061 | Private receivables aging visibility. READ-only policy.
-- No bank settlement, taxes, actual fiscal invoice or production role grants.
BEGIN;
INSERT INTO public.one_permission_templates(permission_key,role,default_allow,risk_class)
VALUES
 ('finance.receivables.view','company_admin',true,'read'),
 ('finance.receivables.view','accountant',true,'read'),
 ('finance.receivables.view','viewer',false,'read')
ON CONFLICT(permission_key,role) DO NOTHING;
INSERT INTO public.one_permission_keys(permission_key)
 VALUES('finance.receivables.view')
ON CONFLICT(permission_key) DO NOTHING;
-- PostgreSQL function one_actor_has_permission(tenant,actor,key) from 048
-- implements reviewed actor/role override and current active membership.
-- Permission templates do NOT grant direct SQL or authorize the browser.
COMMIT;
