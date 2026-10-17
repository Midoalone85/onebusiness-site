-- ONE ERP NEXT / 056 | Read-only permission keys for private operations.
-- These do NOT grant direct SQL to browser users or production connections.
-- Company-scoped policy changes keep the existing 048 independent approvals.
BEGIN;
INSERT INTO one_permission_templates(
 permission_key,role,default_allow,risk_class)
SELECT v.permission_key,t.role,t.default_allow,'read'
FROM (VALUES
 ('sales.order.view'),
 ('purchase.order.view'),
 ('inventory.stock.view'),
 ('inventory.return.view')
) AS v(permission_key)
CROSS JOIN (VALUES
 ('company_admin',true),
 ('accountant',true),
 ('viewer',true)
) AS t(role,default_allow)
ON CONFLICT(permission_key,role) DO NOTHING;
INSERT INTO one_permission_keys(permission_key)
SELECT DISTINCT permission_key FROM one_permission_templates
WHERE permission_key IN (
 'sales.order.view','purchase.order.view',
 'inventory.stock.view','inventory.return.view'
)
ON CONFLICT(permission_key) DO NOTHING;
-- No EXECUTE grants here. The trusted application's restricted report role
-- needs an independently reviewed SELECT/EXECUTE grant in production.
COMMIT;
