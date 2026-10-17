"""Non-mutating release-blocking source and contract checks."""
from pathlib import Path
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
def check():
    missing_main = [p for p in REQUIRED_MAIN if not (ROOT / p).is_file()]
    missing_finance = [p for p in REQUIRED_FINANCE if not (ROOT / p).is_file()]
    for path in missing_main:
        print("MISSING MAIN ASSET:", path)
    for path in missing_finance:
        print("FINANCE NOT INTEGRATED:", path)
    if not (missing_main or missing_finance):
        print("Source presence gates PASS. This is not a security or deployment approval.")
    return 1 if missing_main or missing_finance else 0
if __name__ == "__main__":
    sys.exit(check())
