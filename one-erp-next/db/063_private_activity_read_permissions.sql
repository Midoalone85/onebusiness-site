-- ONE ERP NEXT 063 | Private per-record activity timeline permission (READ only).
-- This does NOT insert events or claim to capture every click/edit.
-- Existing immutable database audit tables remain the source of truth.
-- Actor-specific decisions use 048 membership and independent permission overrides.
BEGIN;
INSERT INTO public.one_permission_templates(permission_key,role,default_allow,risk_class)
VALUES
 ('audit.activity.view','company_admin',true,'read'),
 ('audit.activity.view','accountant',true,'read'),
 ('audit.activity.view','viewer',false,'read')
ON CONFLICT(permission_key,role) DO NOTHING;
INSERT INTO public.one_permission_keys(permission_key)
VALUES('audit.activity.view')
ON CONFLICT(permission_key) DO NOTHING;
-- Do not grant SQL SELECT or identity impersonation to browsers or PUBLIC.
-- Independently review dedicated read role, RLS, membership and tenant isolation.
COMMIT;
