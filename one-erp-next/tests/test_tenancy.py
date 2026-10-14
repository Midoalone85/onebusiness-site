import unittest
from uuid import uuid4
from one_erp_core.tenancy import Action, Membership, Role, can_access


class TenantSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tenant_a, self.tenant_b = uuid4(), uuid4()
        self.admin, self.accountant, self.viewer = uuid4(), uuid4(), uuid4()
        self.memberships = (
            Membership(self.admin, self.tenant_a, Role.COMPANY_ADMIN),
            Membership(self.accountant, self.tenant_a, Role.ACCOUNTANT),
            Membership(self.viewer, self.tenant_b, Role.VIEWER),
        )

    def test_admin_manages_own_company(self):
        self.assertTrue(can_access(actor_id=self.admin, tenant_id=self.tenant_a,
                                   action=Action.MANAGE_USERS, memberships=self.memberships))

    def test_cross_company_access_denied(self):
        for action in Action:
            self.assertFalse(can_access(actor_id=self.admin, tenant_id=self.tenant_b,
                                        action=action, memberships=self.memberships))

    def test_accountant_cannot_manage_users(self):
        self.assertFalse(can_access(actor_id=self.accountant, tenant_id=self.tenant_a,
                                    action=Action.MANAGE_USERS, memberships=self.memberships))

    def test_viewer_cannot_write(self):
        self.assertFalse(can_access(actor_id=self.viewer, tenant_id=self.tenant_b,
                                    action=Action.WRITE_ACCOUNTING, memberships=self.memberships))

    def test_disabled_user_denied(self):
        memberships = (Membership(self.admin, self.tenant_a, Role.COMPANY_ADMIN, is_active=False),)
        self.assertFalse(can_access(actor_id=self.admin, tenant_id=self.tenant_a,
                                    action=Action.READ, memberships=memberships))
