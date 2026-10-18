"""Unit-level fail-closed three-layer SQL acceptance orchestration tests.

These mocks validate orchestration only. Actual privilege and two-tenant
database tests run separately in real disposable PostgreSQL CI.
"""
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import uuid4

from one_erp_core.runtime_reader_privilege_audit import (
    RestrictedReaderPolicy, RestrictedReaderEvidence,
)
from one_erp_core.staging_tenant_db_acceptance import (
    StagingTenantDatabaseBlocked, verify_staging_finance_reader,
)
from one_erp_core.tenant_inventory import ExclusiveTenantInventory
from one_erp_core.tenant_readiness import DatabaseReadinessEvidence


class StagingTenantDBAcceptanceTests(TestCase):
    def setUp(self):
        self.tenant, self.actor = uuid4(), uuid4()
        self.db = "one_ci_isolated_tenant"
        self.reader = "one_ci_reader_login"
        self.peers = ("one_ci_reader_other",)
        self.policy = RestrictedReaderPolicy(
            login_role=self.reader,
            required_tables=frozenset({"one_tenants"}),
            allowed_tables=frozenset({"one_tenants"}),
        )
        self.inspector, self.reader_conn = object(), object()
        self.params = dict(
            privileged_inspector=self.inspector,
            restricted_reader=self.reader_conn,
            expected_tenant=self.tenant,
            expected_actor=self.actor,
            expected_database=self.db,
            expected_reader_role=self.reader,
            forbidden_peer_roles=self.peers,
            reviewed_reader_policy=self.policy,
        )
        self.events = []
        self.inventory = ExclusiveTenantInventory(
            tenant_id=self.tenant, database_name=self.db,
            reader_role=self.reader,
            rejected_peer_roles=self.peers, digest=b"mocked-inventory",
        )
        self.membership = DatabaseReadinessEvidence(
            tenant_id=self.tenant, database_name=self.db,
            role_name=self.reader,
            observed_tables=("one_tenants",),digest=b"mocked-readiness",
        )
        self.grants = RestrictedReaderEvidence(
            database_name=self.db, login_role=self.reader,
            checked_tables=14, checked_functions=26,
            reachable_group_roles=(),
            approved_table_reads=("one_tenants",),
            approved_function_executes=(),
        )

    def run_gates(self,*,failure=None,replace=None,arguments=None):
        replacement=replace or {}
        def fake_inventory(*args,**kwargs):
            self.events.append("inventory")
            if failure == "inventory":
                raise ValueError("secret://fake-admin-password")
            return replacement.get("inventory",self.inventory)
        def fake_membership(*args,**kwargs):
            self.events.append("reader")
            if failure == "reader":
                raise RuntimeError("postgresql://private-user")
            return replacement.get("reader",self.membership)
        def fake_grants(*args,**kwargs):
            self.events.append("acl")
            if failure == "acl":
                raise ValueError("secret-grant-catalog")
            return replacement.get("acl",self.grants)
        with (
            patch("one_erp_core.staging_tenant_db_acceptance.verify_exclusive_tenant_inventory",
                  side_effect=fake_inventory),
            patch("one_erp_core.staging_tenant_db_acceptance.verify_physical_tenant_database",
                  side_effect=fake_membership),
            patch("one_erp_core.staging_tenant_db_acceptance.inspect_restricted_reader",
                  side_effect=fake_grants),
        ):
            return verify_staging_finance_reader(
                **(arguments if arguments is not None else self.params))

    def test_three_independent_gates_required_before_staging_sql_evidence(self):
        report = self.run_gates()
        self.assertEqual(self.events,["inventory","reader","acl"])
        self.assertEqual(report.status,"STAGING_DB_PREFLIGHT_ONLY")
        self.assertFalse(report.route_ready)
        self.assertFalse(report.release_approved)
        self.assertEqual(report.verified_peer_roles,self.peers)
        self.assertEqual(report.catalog_relations_checked,14)
        self.assertEqual(report.approved_table_reads,("one_tenants",))

    def test_each_failure_blocks_and_cannot_expose_credentials(self):
        for stage,expected_prefix in (
            ("inventory",[]),("reader",["inventory"]),("acl",["inventory","reader"]),
        ):
            with self.subTest(stage=stage):
                self.events=[]
                with self.assertRaises(StagingTenantDatabaseBlocked) as cm:
                    self.run_gates(failure=stage)
                self.assertNotIn("secret",str(cm.exception))
                self.assertNotIn("postgresql",str(cm.exception))
                self.assertEqual(self.events,expected_prefix+[stage])

    def test_a_spoofed_success_structure_does_not_count_as_evidence(self):
        for key, forged in (
            ("inventory",{"tenant_id":str(self.tenant)}),
            ("reader",{"role_name":self.reader}),
            ("acl",{"checked_tables":14}),
        ):
            with self.subTest(key=key):
                self.events=[]
                with self.assertRaises(StagingTenantDatabaseBlocked):
                    self.run_gates(replace={key:forged})

    def test_correct_dataclass_from_wrong_company_role_or_database_denied(self):
        from dataclasses import replace as change
        for key, forged in (
            ("inventory",change(self.inventory,tenant_id=uuid4())),
            ("inventory",change(self.inventory,
                                rejected_peer_roles=("one_ci_reader_unchecked",))),
            ("reader",change(self.membership,database_name="one_ci_wrong_database")),
            ("reader",change(self.membership,role_name="one_ci_wrong_login")),
            ("acl",change(self.grants,login_role="one_ci_wrong_reader")),
        ):
            with self.subTest(key=key):
                with self.assertRaises(StagingTenantDatabaseBlocked):
                    self.run_gates(replace={key:forged})

    def test_untrusted_prerequisites_refused_before_any_sql(self):
        examples=[
            {"expected_tenant":uuid4().hex},
            {"expected_tenant":type(self.tenant)(int=0)},
            {"expected_actor":type(self.actor)(int=0)},
            {"expected_database":"different-db;drop database"},
            {"expected_reader_role":"postgres"},
            {"forbidden_peer_roles":()},
            {"forbidden_peer_roles":(self.reader,)},
            {"forbidden_peer_roles":(self.peers[0],self.peers[0])},
            {"reviewed_reader_policy":RestrictedReaderPolicy(
                login_role="other_reader",
                required_tables=frozenset({"one_tenants"}),
                allowed_tables=frozenset({"one_tenants"}))},
            {"restricted_reader":self.inspector},
            {"privileged_inspector":None},
        ]
        for overrides in examples:
            with self.subTest(overrides=str(overrides)):
                self.events=[]
                params=dict(self.params,**overrides)
                with self.assertRaises(StagingTenantDatabaseBlocked):
                    self.run_gates(arguments=params)
                self.assertEqual(self.events,[])

    def test_server_supplied_exact_identifiers_are_forwarded_to_each_gate(self):
        with (
            patch("one_erp_core.staging_tenant_db_acceptance.verify_exclusive_tenant_inventory",
                  return_value=self.inventory) as scan,
            patch("one_erp_core.staging_tenant_db_acceptance.verify_physical_tenant_database",
                  return_value=self.membership) as read,
            patch("one_erp_core.staging_tenant_db_acceptance.inspect_restricted_reader",
                  return_value=self.grants) as acl,
        ):
            verify_staging_finance_reader(**self.params)
        self.assertEqual(scan.call_args.kwargs["expected_database"],self.db)
        self.assertEqual(scan.call_args.kwargs["forbidden_peer_roles"],self.peers)
        self.assertIs(scan.call_args.args[0],self.inspector)
        self.assertIs(read.call_args.args[0],self.reader_conn)
        self.assertEqual(read.call_args.kwargs["expected_actor"],self.actor)
        self.assertIs(acl.call_args.args[0],self.inspector)
        self.assertIs(acl.call_args.kwargs["policy"],self.policy)
