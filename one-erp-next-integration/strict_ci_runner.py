"""Strict, NON-PRODUCTION finance CI acceptance.

Runs all existing Python/real-PostgreSQL suites once, then rejects silent
skips, lost critical acceptance cases, collection errors or insufficient
coverage. This is a disposable GitHub CI gate, NOT a migration/deploy command.
No external/customer DB may ever be connected by this script.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest

PROJECT = Path(__file__).resolve().parents[1] / "one-erp-next"
MIN_CASES = 700
REQUIRED = frozenset({
    "test_postgres_integration.PostgreSQLIntegrationTests."
    "test_cross_company_visibility_is_denied",
    "test_postgres_integration.PostgreSQLIntegrationTests."
    "test_atomic_goods_receipt_sale_value_and_audit_are_single_transaction",
    "test_approved_finance_sources_postgres.ApprovedSourcePostgresTests."
    "test_full_purchase_receipt_grni_dispatch_cogs_and_invoice_journal",
    "test_approved_finance_sources_postgres.ApprovedSourcePostgresTests."
    "test_real_invoice_to_private_readonly_wsgi_pilot_chain",
    "test_approved_finance_sources_postgres.ApprovedSourcePostgresTests."
    "test_062_real_pg_reviewer_source_restricted_role_and_approval_lifecycle",
    "test_runtime_reader_privilege_audit_postgres."
    "RuntimeReadOnlyRoleAuditPostgresTests."
    "test_missing_forced_rls_on_reader_table_is_release_blocking",
    "test_staging_tenant_db_acceptance.StagingTenantDBAcceptanceTests."
    "test_three_independent_gates_required_before_staging_sql_evidence",
    "test_staging_smoke.StagingSmokeTests."
    "test_pilot_access_denials_block_cross_tenant_data_leak",
    "test_staging_smoke.StagingSmokeTests."
    "test_pilot_access_denials_block_if_anonymous_read_is_exposed",
})


def validate_ci_db(env: dict, params: dict) -> list[str]:
    """Pure, strict host/database check before ANY test has opened a DB."""
    problems = []
    if env.get("CI") != "true":
        problems.append("Requires GitHub CI disposable database environment")
    if (params.get("dbname") != "one_erp_test" or
            params.get("host") not in ("localhost", "127.0.0.1", "::1") or
            params.get("port", "5432") != "5432" or
            params.get("hostaddr") or params.get("service") or
            params.get("servicefile")):
        problems.append("Refusing a non-loopback or non-disposable test database")
    # libpq also reads process environment defaults. PGHOSTADDR can direct
    # traffic to a remote IP even when the DSN explicitly says host=localhost.
    # Service files and startup options also bypass the reviewed CI contract.
    if any(env.get(key) for key in (
            "PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS")):
        problems.append("Unreviewed libpq environment overrides are forbidden")
    if (env.get("ONE_TEST_PG_HOST") != "localhost" or
            env.get("ONE_TEST_PG_PORT") != "5432" or
            env.get("ONE_TEST_PG_DB") != "one_erp_test"):
        problems.append("CI database environment does not match expected fixture")
    return problems


def collected_test_ids(suite) -> list[str]:
    """Flatten discovered tests without executing or mutating the suite."""
    ids = []
    def walk(node):
        if isinstance(node, unittest.TestSuite):
            for child in node:
                walk(child)
        elif isinstance(node, unittest.case.TestCase):
            ids.append(node.id())
        else:
            raise ValueError("Unrecognized test in finance suite")
    walk(suite)
    return ids


def audit_result(collected: list[str], result, *,
                 minimum: int = MIN_CASES,
                 required: frozenset[str] = REQUIRED) -> list[str]:
    """Block CI success if an important test was absent or silently skipped."""
    problems = []
    if len(collected) < minimum:
        problems.append("Collected too few tests for the reviewed baseline")
    if len(set(collected)) != len(collected):
        problems.append("Duplicate test identifiers in CI discovery")
    if required - set(collected):
        problems.append("Critical real-DB/security acceptance tests missing")
    if result.testsRun != len(collected):
        problems.append("Not every discovered test executed")
    if result.skipped:
        problems.append("Skipped tests are forbidden in strict finance CI")
    if getattr(result, "expectedFailures", []):
        problems.append("Expected failures are not permitted as success")
    if not result.wasSuccessful():
        problems.append("Finance suite contains failures or errors")
    return problems


def main() -> int:
    """Safe, explicitly CI-only entrypoint. Never prints test DSN/secrets."""
    try:
        import psycopg
        from psycopg.conninfo import conninfo_to_dict
        parsed = conninfo_to_dict(os.environ.get("ONE_TEST_PG_DSN", ""))
    except Exception:
        print("STRICT_CI_BLOCKED: Cannot inspect database connection safely",
              file=sys.stderr)
        return 2
    errors = validate_ci_db(os.environ, parsed)
    if errors:
        for problem in errors:
            print("STRICT_CI_BLOCKED: " + problem, file=sys.stderr)
        return 2

    sys.path.insert(0, str(PROJECT / "src"))
    tests_dir = PROJECT / "tests"
    loader = unittest.TestLoader()
    try:
        suite = loader.discover(
            start_dir=str(tests_dir), pattern="test*.py",
            top_level_dir=str(tests_dir))
        identifiers = collected_test_ids(suite)
    except Exception:
        print("STRICT_CI_BLOCKED: Cannot collect Python tests", file=sys.stderr)
        return 2

    if loader.errors or len(identifiers) < MIN_CASES or REQUIRED - set(identifiers):
        print("STRICT_CI_BLOCKED: Test discovery incomplete or mandatory "
              "acceptance test absent", file=sys.stderr)
        return 2

    result = unittest.TextTestRunner(verbosity=2).run(suite)
    problems = audit_result(identifiers, result)
    if problems:
        for problem in problems:
            print("STRICT_CI_BLOCKED: " + problem, file=sys.stderr)
        return 1
    print("STRICT_CI_PASS: all " + str(result.testsRun) +
          " tests executed, zero skips, all mandatory finance, staging and "
          "PostgreSQL security acceptance cases passed")
    print("SOURCE_ONLY: no Oracle deployment or commercial approval")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
