"""Unit tests for independent, high-safety ONE ERP dynamic settings."""
import unittest
from dataclasses import replace
from uuid import uuid4

from one_erp_core.dynamic_policies import (
    INITIAL_POLICY_CATALOG, PolicyChange, PolicyDefinition,
    PolicyError, PolicyState, Risk, Scope, preview_policy_change,
    prepare_approved_policy_change,
)
from one_erp_core.tenancy import Membership, Role


class DynamicPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tenant, self.other, self.maker, self.reviewer, self.admin2 = (
            uuid4() for _ in range(5)
        )
        self.members = (
            Membership(self.maker, self.tenant, Role.COMPANY_ADMIN),
            Membership(self.reviewer, self.tenant, Role.ACCOUNTANT),
            Membership(self.admin2, self.tenant, Role.COMPANY_ADMIN),
            Membership(uuid4(), self.other, Role.COMPANY_ADMIN),
        )
        self.low, self.high = INITIAL_POLICY_CATALOG[0], INITIAL_POLICY_CATALOG[1]

    def change(self, spec, *, value=False, approvals=(), revision=4, scope=Scope.COMPANY, branch_id=None):
        return PolicyChange(self.tenant, spec.key, value, revision, self.maker,
                            scope, branch_id, approvals)

    def state(self, spec, *, scope=Scope.COMPANY, branch_id=None):
        return PolicyState(self.tenant, spec.key, spec.default_value, 4, scope, branch_id)

    def test_low_risk_dry_run_is_pure_and_prepare_has_audit_intent(self):
        state = self.state(self.low)
        change = self.change(self.low)
        preview = preview_policy_change(self.low, state, change, memberships=self.members)
        self.assertTrue(preview.can_apply)
        self.assertEqual(state.revision, 4)
        new, audit = prepare_approved_policy_change(
            self.low, state, change, memberships=self.members)
        self.assertEqual((new.revision, new.value), (5, False))
        self.assertEqual((audit.old_revision, audit.new_revision), (4, 5))
        self.assertEqual(state.value, True)

    def test_high_risk_needs_distinct_independent_approvals(self):
        state = self.state(self.high)
        for approvals in ((), (self.reviewer,), (self.maker, self.admin2),
                          (self.reviewer, self.reviewer)):
            with self.subTest(approvals=approvals):
                preview = preview_policy_change(
                    self.high, state, self.change(self.high, approvals=approvals),
                    memberships=self.members)
                self.assertFalse(preview.can_apply)
        approved = self.change(self.high, approvals=(self.reviewer, self.admin2))
        self.assertTrue(preview_policy_change(
            self.high, state, approved, memberships=self.members).can_apply)

    def test_stale_version_and_other_company_block(self):
        state = self.state(self.low)
        self.assertIn("stale_revision", preview_policy_change(
            self.low, state, self.change(self.low, revision=3),
            memberships=self.members).reasons)
        other_change = replace(self.change(self.low), tenant_id=self.other)
        self.assertFalse(preview_policy_change(
            self.low, state, other_change, memberships=self.members).can_apply)

    def test_bool_does_not_equal_integer_or_string(self):
        state = self.state(self.low)
        for value in (0, "false", "0"):
            with self.subTest(value=value):
                self.assertIn("proposed_value_not_allowed", preview_policy_change(
                    self.low, state, self.change(self.low, value=value),
                    memberships=self.members).reasons)

    def test_inactive_or_viewer_cannot_request_change(self):
        state = self.state(self.low)
        for role in (Role.VIEWER, Role.ACCOUNTANT):
            memberships = (Membership(self.maker, self.tenant, role),)
            self.assertIn("requester_not_authorized", preview_policy_change(
                self.low, state, self.change(self.low),
                memberships=memberships).reasons)
        self.assertIn("requester_not_authorized", preview_policy_change(
            self.low, state, self.change(self.low),
            memberships=(Membership(self.maker, self.tenant, Role.COMPANY_ADMIN, False),)
        ).reasons)

    def test_branch_override_scoped_and_explicit(self):
        branch = uuid4()
        state = self.state(self.low, scope=Scope.BRANCH, branch_id=branch)
        correct = self.change(self.low, scope=Scope.BRANCH, branch_id=branch)
        self.assertTrue(preview_policy_change(
            self.low, state, correct, memberships=self.members).can_apply)
        invalid = replace(correct, branch_id=uuid4())
        self.assertFalse(preview_policy_change(
            self.low, state, invalid, memberships=self.members).can_apply)
        no_branch_override = PolicyDefinition("finance.close_policy", (True, False), True, Risk.HIGH)
        other_state = self.state(no_branch_override, scope=Scope.BRANCH, branch_id=branch)
        other_change = self.change(no_branch_override, scope=Scope.BRANCH,
                                   branch_id=branch, approvals=(self.reviewer, self.admin2))
        self.assertIn("branch_override_forbidden", preview_policy_change(
            no_branch_override, other_state, other_change, memberships=self.members).reasons)

    def test_critical_and_hard_accounting_safety_cannot_be_disabled(self):
        critical = INITIAL_POLICY_CATALOG[3]
        state = self.state(critical)
        change = self.change(critical, value=False, approvals=(self.reviewer, self.admin2))
        with self.assertRaises(PolicyError):
            prepare_approved_policy_change(critical, state, change,
                                           memberships=self.members)

    def test_reject_invalid_policy_catalog_and_scope(self):
        with self.assertRaises(PolicyError):
            PolicyDefinition("bad key", (True,), True, Risk.LOW)
        with self.assertRaises(PolicyError):
            PolicyDefinition("x.y", (1, 1), 1, Risk.LOW)
        with self.assertRaises(PolicyError):
            PolicyState(self.tenant, self.low.key, True, 0, Scope.BRANCH)


if __name__ == "__main__":
    unittest.main()
