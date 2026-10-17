"""Tests for release gate itself, so unsafe weakening is caught in CI."""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "release_gates", Path(__file__).with_name("verify_integration.py"))
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

class ReleaseGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.addCleanup(patch.object(gate, "ROOT", self.root).stop)
        patch.object(gate, "ROOT", self.root).start()

    def migration(self, filename, sql="SELECT 1;"):
        directory = self.root / "one-erp-next" / "db"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / filename).write_text(sql, encoding="utf-8")

    def test_migration_sequence_gaps_allowed(self):
        self.migration("001_initial.sql")
        self.migration("003_later.sql")
        self.assertEqual(gate.migration_checks(), [])

    def test_duplicate_sequence_rejected(self):
        self.migration("001_initial.sql")
        self.migration("001_collision.sql")
        self.assertTrue(any("Duplicate migration sequence" in e for e in gate.migration_checks()))

    def test_empty_sql_rejected(self):
        self.migration("001_initial.sql", "  ")
        self.assertTrue(any("Empty migration" in e for e in gate.migration_checks()))

    def test_zero_sequence_rejected(self):
        self.migration("000_invalid.sql")
        self.assertTrue(any("Zero migration number" in e for e in gate.migration_checks()))

    def test_missing_migrations_rejected(self):
        self.assertTrue(gate.migration_checks())

    def roles(self, sql):
        d = self.root / "one-erp-next" / "ops"
        d.mkdir(parents=True, exist_ok=True)
        for filename in ("finance_command_privileges.sql", "report_reader_privileges.sql"):
            (d / filename).write_text(sql, encoding="utf-8")

    def test_restricted_roles_allowed(self):
        self.roles("BEGIN; CREATE ROLE demo NOLOGIN NOSUPERUSER NOBYPASSRLS; COMMIT;")
        self.assertEqual(gate.privilege_template_checks(), [])

    def test_privileged_role_rejected(self):
        self.roles("BEGIN; CREATE ROLE demo NOLOGIN SUPERUSER NOBYPASSRLS; COMMIT;")
        self.assertTrue(any("Unexpected privileged" in e for e in gate.privilege_template_checks()))

    def test_broad_grant_rejected(self):
        self.roles("BEGIN; CREATE ROLE demo NOLOGIN NOSUPERUSER NOBYPASSRLS; GRANT ALL ON ALL TABLES IN SCHEMA public TO demo; COMMIT;")
        self.assertTrue(any("Unbounded GRANT ALL" in e for e in gate.privilege_template_checks()))

if __name__ == "__main__":
    unittest.main()
