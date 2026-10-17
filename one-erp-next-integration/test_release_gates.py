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

    def test_alter_role_superuser_rejected(self):
        self.roles("BEGIN; CREATE ROLE demo NOLOGIN NOSUPERUSER NOBYPASSRLS; "
                   "ALTER ROLE demo WITH SUPERUSER; COMMIT;")
        self.assertTrue(any("Unexpected privileged" in e
                            for e in gate.privilege_template_checks()))

    def test_login_role_rejected(self):
        self.roles("BEGIN; CREATE ROLE demo LOGIN NOSUPERUSER NOBYPASSRLS; COMMIT;")
        issues = gate.privilege_template_checks()
        self.assertTrue(any("Unexpected privileged" in e for e in issues))
        self.assertTrue(any("Missing minimum role protections" in e for e in issues))

    def test_comments_cannot_supply_no_login_or_rls_guards(self):
        self.roles("BEGIN; -- NOLOGIN NOBYPASSRLS\n"
                   "CREATE ROLE demo LOGIN NOSUPERUSER BYPASSRLS; COMMIT;")
        self.assertTrue(any("Missing minimum role protections" in e
                            for e in gate.privilege_template_checks()))

    def test_commented_create_role_does_not_qualify(self):
        self.roles("BEGIN;\n-- CREATE ROLE demo NOLOGIN NOBYPASSRLS;\nCOMMIT;")
        self.assertTrue(any("Missing minimum role protections" in e
                            for e in gate.privilege_template_checks()))

    def test_select_on_all_tables_rejected(self):
        self.roles("BEGIN; CREATE ROLE demo NOLOGIN NOSUPERUSER NOBYPASSRLS; "
                   "GRANT SELECT ON ALL TABLES IN SCHEMA public TO demo; COMMIT;")
        self.assertTrue(any("Unbounded object-class GRANT" in e
                            for e in gate.privilege_template_checks()))

    def test_public_grant_rejected(self):
        self.roles("BEGIN; CREATE ROLE demo NOLOGIN NOSUPERUSER NOBYPASSRLS; "
                   "GRANT SELECT ON TABLE data TO PUBLIC; COMMIT;")
        self.assertTrue(any("GRANT to PUBLIC" in e
                            for e in gate.privilege_template_checks()))

    def test_commented_bulk_and_public_grants_allowed(self):
        self.roles("BEGIN; CREATE ROLE demo NOLOGIN NOSUPERUSER NOBYPASSRLS;\n"
                   "-- GRANT SELECT ON ALL TABLES IN SCHEMA public TO demo;\n"
                   "-- GRANT SELECT ON TABLE data TO PUBLIC;\nCOMMIT;")
        self.assertEqual(gate.privilege_template_checks(), [])

    def test_multiline_grant_all_rejected(self):
        self.roles("BEGIN; CREATE ROLE demo NOLOGIN NOSUPERUSER NOBYPASSRLS;\n"
                   "GRANT\n ALL\n PRIVILEGES\n ON TABLE data TO demo; COMMIT;")
        self.assertTrue(any("Unbounded GRANT ALL" in e
                            for e in gate.privilege_template_checks()))

    def test_commented_grant_all_is_not_executable(self):
        self.roles("BEGIN; CREATE ROLE demo NOLOGIN NOSUPERUSER NOBYPASSRLS;\n"
                   "-- GRANT ALL ON ALL TABLES IN SCHEMA public TO demo;\nCOMMIT;")
        self.assertEqual(gate.privilege_template_checks(), [])

    def test_broad_grant_rejected(self):
        self.roles("BEGIN; CREATE ROLE demo NOLOGIN NOSUPERUSER NOBYPASSRLS; GRANT ALL ON ALL TABLES IN SCHEMA public TO demo; COMMIT;")
        self.assertTrue(any("Unbounded GRANT ALL" in e for e in gate.privilege_template_checks()))

if __name__ == "__main__":
    unittest.main()
