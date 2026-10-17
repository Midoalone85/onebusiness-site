"""Non-mutating release-blocking source and contract checks."""
from pathlib import Path
import re
import sys
ROOT = Path(__file__).resolve().parents[1]
REQUIRED_MAIN = [
    ".github/workflows/one-erp-brand-guard.yml",
    ".github/workflows/one-erp-preview-tests.yml",
    ".github/workflows/one-erp-oracle-bridge.yml",
    "one-erp-preview/index.html",
    "one-erp-preview/ui.js",
    "one-erp-preview/ui.css",
    "one-erp-preview/workspace/index.html",
    "one-erp-preview/tests/navigation.test.cjs",
    "DOMAIN_SETUP.md",
]
REQUIRED_FINANCE = [
    "one-erp-next/src/one_erp_core/accounting.py",
    "one-erp-next/tests/test_postgres_integration.py",
    ".github/workflows/one-erp-next-finance-tests.yml",
]
def migration_checks():
    """Read-only numbering guard. SQL semantics are exercised by PG integration tests."""
    migration_dir = ROOT / "one-erp-next" / "db"
    migrations = sorted(migration_dir.glob("[0-9][0-9][0-9]_*.sql"))
    errors = []
    if not migrations:
        return ["No numbered PostgreSQL migrations found"]
    numbers = {}
    seen_casefold = set()
    for path in migrations:
        number = int(path.name[:3])
        if path.name.casefold() in seen_casefold:
            errors.append(f"Case-insensitive duplicate migration filename: {path.name}")
        seen_casefold.add(path.name.casefold())
        if number == 0:
            errors.append(f"Zero migration number: {path.name}")
        numbers.setdefault(number, []).append(path.name)
        if not path.read_text(encoding="utf-8").strip():
            errors.append(f"Empty migration: {path.name}")
    for number, files in sorted(numbers.items()):
        if len(files) > 1:
            errors.append(f"Duplicate migration sequence {number:03d}: {files}")
    if min(numbers) != 1:
        errors.append(f"Migration series does not start with 001 (starts at {min(numbers):03d})")
    gaps = sorted(set(range(min(numbers), max(numbers) + 1)) - set(numbers))
    if gaps:
        print(f"RESERVED/UNUSED migration numbers (review): {gaps}")
    print(f"PostgreSQL migrations checked: {len(migrations)}, range {min(numbers):03d}-{max(numbers):03d}")
    return errors

def privilege_template_checks():
    """Static safety preflight only; PostgreSQL permissions need live role tests."""
    scripts = [
        ROOT / "one-erp-next/ops/finance_command_privileges.sql",
        ROOT / "one-erp-next/ops/report_reader_privileges.sql",
    ]
    errors = []
    for path in scripts:
        if not path.is_file():
            errors.append(f"Missing reviewed privilege template: {path.name}")
            continue
        sql = path.read_text(encoding="utf-8")
        # Ignore SQL comments before inspecting executable permissions. Neither
        # role protections nor dangerous grants may be inferred from comments.
        executable = re.sub(r"--[^\n]*", "", sql)
        executable = re.sub(r"/\*[\s\S]*?\*/", "", executable)
        upper = executable.upper()
        created_roles = re.findall(r"\bCREATE\s+ROLE\b[^;]*;", upper)
        if not created_roles or not all(
                re.search(r"\bNOLOGIN\b", stmt) and re.search(r"\bNOBYPASSRLS\b", stmt)
                for stmt in created_roles):
            errors.append(f"Missing minimum role protections: {path.name}")
        # CREATE ROLE and subsequent ALTER ROLE must both remain least-privilege.
        # Negative flags (NOSUPERUSER, NOBYPASSRLS etc.) are not privileges.
        for stmt in re.findall(r"\b(?:CREATE|ALTER)\s+ROLE\b[^;]*;", upper):
            if re.search(
                r"(?<![A-Z_])(?:SUPERUSER|BYPASSRLS|CREATEDB|CREATEROLE|LOGIN)(?![A-Z_])",
                stmt,
            ):
                errors.append(f"Unexpected privileged role flag: {path.name}")
        if re.search(r"\bGRANT\s+ALL\b", upper):
            errors.append(f"Unbounded GRANT ALL detected: {path.name}")
        # Named roles may receive only reviewed object-scoped privileges.
        # Bulk grants or privileges to PUBLIC bypass the allowlist review.
        if re.search(r"\bGRANT\b[^;]*\bON\s+ALL\s+(?:TABLES|SEQUENCES|FUNCTIONS|ROUTINES)\b", upper):
            errors.append(f"Unbounded object-class GRANT detected: {path.name}")
        if re.search(r"\bGRANT\b[^;]*\bTO\s+PUBLIC\b", upper):
            errors.append(f"GRANT to PUBLIC detected: {path.name}")
        if "BEGIN;" not in upper or "COMMIT;" not in upper:
            errors.append(f"Missing transactional scope: {path.name}")
    return errors

def check():
    missing_main = [p for p in REQUIRED_MAIN if not (ROOT / p).is_file()]
    missing_finance = [p for p in REQUIRED_FINANCE if not (ROOT / p).is_file()]
    for path in missing_main:
        print("MISSING MAIN ASSET:", path)
    for path in missing_finance:
        print("FINANCE NOT INTEGRATED:", path)
    migration_errors = migration_checks()
    migration_errors.extend(privilege_template_checks())
    for issue in migration_errors:
        print("MIGRATION GATE:", issue)
    if not (missing_main or missing_finance or migration_errors):
        print("Source presence gates PASS. This is not a security or deployment approval.")
    return 1 if missing_main or missing_finance or migration_errors else 0
if __name__ == "__main__":
    sys.exit(check())
