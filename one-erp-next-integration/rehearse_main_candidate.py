"""Read-only Git tree audit for a prospective ONE ERP NEXT main merge.

No branch ref updates, commits, merges, network access, credentials, DB changes
or deployments. Invoked by CI only after explicit non-secret Git fetches.
This is a source-preservation check, NOT release, security or DBA approval.
"""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PROTECTED_MAIN = (
    ".github/workflows/one-erp-brand-guard.yml",
    ".github/workflows/one-erp-oracle-bridge.yml",
    ".github/workflows/one-erp-preview-tests.yml",
    "one-erp-preview/index.html",
    "one-erp-preview/ui.js",
    "one-erp-preview/ui.css",
    "one-erp-preview/workspace/index.html",
    "one-erp-preview/tests/navigation.test.cjs",
    "DOMAIN_SETUP.md",
)
# Seven byte-identical files verified across the sibling invoice-review branch.
INVOICE_REVIEW_PRESERVED = (
    "one-erp-next/db/062_invoice_source_review_permission.sql",
    "one-erp-next/ops/INVOICE_REVIEW_SOURCE_062_20261017.md",
    "one-erp-next/private-ui/invoice-review-client.test.mjs",
    "one-erp-next/src/one_erp_core/invoice_review_source.py",
    "one-erp-next/src/one_erp_core/invoice_review_wsgi.py",
    "one-erp-next/tests/test_invoice_review_source.py",
    "one-erp-next/tests/test_invoice_review_wsgi.py",
)
AR_PRESERVED = (
    "one-erp-next/private-ui/receivables-client.mjs",
    "one-erp-next/private-ui/receivables-client.test.mjs",
    "one-erp-next/private-ui/receivables-page.mjs",
)
RETAINED_PYTHON_TESTS = (
    "one-erp-next/tests/test_approved_finance_sources_postgres.py",
    "one-erp-next/tests/test_staging_router.py",
)
REVIEW_REQUIRED = (
    "one-erp-next/private-ui/invoice-approval-page.mjs",
    "one-erp-next/private-ui/invoice-review-client.mjs",
    "one-erp-next/src/one_erp_core/staging_router.py",
    *RETAINED_PYTHON_TESTS,
)
_SAFE_REF = re.compile(r"[A-Za-z0-9_./-]+\\Z")


class GateFailure(RuntimeError):
    pass


def checked_ref(value: str) -> str:
    if (not _SAFE_REF.fullmatch(value) or value.startswith("-") or
            ".." in value or "//" in value or value.endswith("/")):
        raise GateFailure("Untrusted Git ref; use an explicit fetched branch/commit")
    return value


class GitReader:
    """All methods are Git read-only except writing ephemeral merge-tree objects."""
    def __init__(self, directory: Path):
        self.directory = directory

    def _call(self, *args: str, allow_nonzero=False) -> subprocess.CompletedProcess:
        result = subprocess.run(
            ["git", "-C", str(self.directory), *args],
            text=True, capture_output=True, check=False,
            timeout=90,
        )
        if result.returncode and not allow_nonzero:
            raise GateFailure("Git tree/read check failed: " + args[0])
        return result

    def verify(self, ref: str) -> None:
        self._call("rev-parse", "--verify", ref + "^{commit}")

    def ancestor(self, main: str, candidate: str) -> bool:
        value = self._call("merge-base", "--is-ancestor", main, candidate, allow_nonzero=True)
        if value.returncode not in (0, 1):
            raise GateFailure("Could not establish Git ancestry")
        return value.returncode == 0

    def merge_tree(self, main: str, candidate: str) -> str:
        # Only creates unreachable Git tree objects; never updates refs, worktree,
        # index or deploys code. Fail on conflicts/unknown merge states.
        output = self._call("merge-tree", "--write-tree", main, candidate)
        tree = output.stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{40,64}", tree):
            raise GateFailure("Merge tree output must be one Git tree ID")
        return tree

    def tree(self, ref: str) -> str:
        return self._call("rev-parse", "--verify", ref + "^{tree}").stdout.strip()

    def blob(self, ref: str, filename: str) -> str:
        return self._call("rev-parse", "--verify", f"{ref}:{filename}").stdout.strip()

    def source(self, ref: str, filename: str) -> str:
        return self._call("show", f"{ref}:{filename}").stdout


def test_names(source: str) -> set[str]:
    try:
        syntax = ast.parse(source)
    except SyntaxError as error:
        raise GateFailure("Python acceptance test file has invalid syntax") from error
    return {node.name for node in ast.walk(syntax)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")}


def audit(reader, *, main: str, candidate: str, invoice: str, ar: str) -> dict:
    if not reader.ancestor(main, candidate):
        raise GateFailure("Main has commits outside candidate; re-review and rebase safely")
    merged_tree = reader.merge_tree(main, candidate)
    if merged_tree != reader.tree(candidate):
        raise GateFailure("Unexpected merge result differs from audited candidate tree")
    for path in PROTECTED_MAIN:
        expected = reader.blob(main, path)
        if reader.blob(candidate, path) != expected or reader.blob(merged_tree, path) != expected:
            raise GateFailure("Protected main asset would change: " + path)
    for path in INVOICE_REVIEW_PRESERVED:
        if reader.blob(invoice, path) != reader.blob(candidate, path):
            raise GateFailure("Invoice-review sibling artifact changed: " + path)
    for path in AR_PRESERVED:
        if reader.blob(ar, path) != reader.blob(candidate, path):
            raise GateFailure("Receivables sibling artifact changed: " + path)
    retained = 0
    for path in RETAINED_PYTHON_TESTS:
        earlier = test_names(reader.source(invoice, path))
        now = test_names(reader.source(candidate, path))
        if not earlier:
            raise GateFailure("No independent original tests to preserve: " + path)
        missing = sorted(earlier - now)
        if missing:
            raise GateFailure("Original reviewer-branch regression test removed: " +
                              path + ": " + ", ".join(missing))
        retained += len(earlier)
    # These five files differ from one sibling and require HUMAN semantic review.
    return {
        "status": "SOURCE_PRESERVATION_PASS_NOT_RELEASE_APPROVAL",
        "merged_tree": merged_tree,
        "protected_main_assets": len(PROTECTED_MAIN),
        "invoice_review_exact_blobs": len(INVOICE_REVIEW_PRESERVED),
        "receivables_exact_blobs": len(AR_PRESERVED),
        "original_python_test_names_retained": retained,
        "human_review_required": list(REVIEW_REQUIRED),
        "main_merged": False,
        "oracle_deployed": False,
        "release_approved": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--main", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--invoice", required=True)
    parser.add_argument("--ar", required=True)
    args = parser.parse_args()
    try:
        refs = {key: checked_ref(value) for key, value in vars(args).items()}
        reader = GitReader(ROOT)
        for value in refs.values():
            reader.verify(value)
        result = audit(reader, **refs)
    except (GateFailure, subprocess.TimeoutExpired) as error:
        print(json.dumps({"status": "SOURCE_PRESERVATION_BLOCKED",
                          "reason": str(error), "release_approved": False}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
