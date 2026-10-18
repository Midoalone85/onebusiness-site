"""Unit tests for the strict CI runner. No SQL, network or cloud access."""
import unittest
from types import SimpleNamespace

import strict_ci_runner as gate


class SampleCase(unittest.TestCase):
    def test_one(self):
        pass

    def test_two(self):
        pass


class StrictCIGateTests(unittest.TestCase):
    @staticmethod
    def fake_result(*, run=2, skips=(), success=True, expected=()):
        return SimpleNamespace(
            testsRun=run, skipped=list(skips), expectedFailures=list(expected),
            wasSuccessful=lambda: success,
        )

    @staticmethod
    def valid_env():
        return {
            "CI": "true", "ONE_TEST_PG_HOST": "localhost",
            "ONE_TEST_PG_PORT": "5432", "ONE_TEST_PG_DB": "one_erp_test",
        }

    @staticmethod
    def valid_db():
        return {"dbname": "one_erp_test", "host": "localhost", "port": "5432"}

    def test_valid_disposable_ci_contract(self):
        self.assertEqual(
            gate.validate_ci_db(self.valid_env(), self.valid_db()), [])

    def test_untrusted_oracle_or_remote_postgres_is_rejected(self):
        cases = (
            {"host": "prod.pg.example.com"},
            {"host": "10.0.0.3"},
            {"dbname": "customer"},
            {"hostaddr": "10.0.0.3"},
            {"port": "5433"},
            {"service": "production"},
            {"servicefile": "/tmp/service"},
            {"host": "unix-socket"},
        )
        for updates in cases:
            with self.subTest(updates=updates):
                db = {**self.valid_db(), **updates}
                self.assertTrue(gate.validate_ci_db(self.valid_env(), db))

    def test_missing_ci_or_unexpected_environment_refused(self):
        for key, value in (
            ("CI", "false"), ("ONE_TEST_PG_HOST", "remote"),
            ("ONE_TEST_PG_PORT", "5433"),
            ("ONE_TEST_PG_DB", "customer"),
        ):
            env = self.valid_env()
            env[key] = value
            with self.subTest(key=key):
                self.assertTrue(gate.validate_ci_db(env, self.valid_db()))

    def test_collector_retains_exact_case_identifiers(self):
        suite = unittest.TestSuite((
            unittest.TestSuite((SampleCase("test_one"),)),
            SampleCase("test_two"),
        ))
        self.assertEqual(gate.collected_test_ids(suite), [
            SampleCase("test_one").id(), SampleCase("test_two").id(),
        ])

    def test_exact_completed_suite_is_accepted(self):
        self.assertEqual(gate.audit_result(
            ["test_one", "test_two"], self.fake_result(),
            minimum=2, required=frozenset({"test_one", "test_two"})), [])

    def test_skipped_cases_block_even_if_unittest_reports_success(self):
        problems = gate.audit_result(
            ["test_one", "test_two"],
            self.fake_result(skips=[("test_two", "db unavailable")]),
            minimum=2, required=frozenset({"test_one", "test_two"}))
        self.assertTrue(any("Skipped" in problem for problem in problems))

    def test_missing_critical_db_case_blocks_even_if_total_is_high(self):
        cases = ["unrelated_one", "unrelated_two"]
        problems = gate.audit_result(
            cases, self.fake_result(), minimum=2,
            required=frozenset({"required_postgres_security"}))
        self.assertTrue(any("Critical" in problem for problem in problems))

    def test_undercollection_duplicate_partial_and_failed_runs_block(self):
        cases = ["test_one", "test_two"]
        options = (
            (["test_one", "test_two"], self.fake_result(), 3),
            (["test_one", "test_one"], self.fake_result(), 2),
            (cases, self.fake_result(run=1), 2),
            (cases, self.fake_result(success=False), 2),
            (cases, self.fake_result(expected=[("test_one", "known failure")]), 2),
        )
        for collected, result, minimum in options:
            with self.subTest(collected=collected, minimum=minimum):
                self.assertTrue(gate.audit_result(
                    collected, result, minimum=minimum, required=frozenset()))

    def test_unrecognized_discovery_entries_block(self):
        with self.assertRaisesRegex(ValueError, "Unrecognized"):
            gate.collected_test_ids(unittest.TestSuite((lambda: None,)))


if __name__ == "__main__":
    unittest.main()
