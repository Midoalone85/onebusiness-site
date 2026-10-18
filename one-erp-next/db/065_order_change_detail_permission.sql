-- ONE ERP NEXT 065 | Distinct approved privilege for commercial diff disclosure.
-- Source-only / NOT deployed. No automatic GRANT and no PUBLIC access.
BEGIN;
INSERT INTO public.one_permission_templates(permission_key,role,default_allow,risk_class)
VALUES
 ('audit.order_changes.view','company_admin',true,'finance'),
 ('audit.order_changes.view','accountant',false,'finance'),
 ('audit.order_changes.view','viewer',false,'finance')
ON CONFLICT(permission_key,role) DO NOTHING;
INSERT INTO public.one_permission_keys(permission_key)
VALUES('audit.order_changes.view')
ON CONFLICT(permission_key) DO NOTHING;
-- Backend must check exact tenant + actor + sales/purchases read AND this key.
-- Field-level change details remain confidential even when timeline is visible.
COMMIT;
