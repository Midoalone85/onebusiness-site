"""Non-network, non-destructive release-gate regression tests."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest

script = Path(__file__).with_name("rehearse_main_candidate.py")
spec = importlib.util.spec_from_file_location("main_candidate_gate", script)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class FakeGit:
    def __init__(self):
        self.is_ancestor = True
        self.candidate_tree = "ab" * 20
        self.merged = self.candidate_tree
        self.blobs = {}
        self.sources = {}
        for path in gate.PROTECTED_MAIN:
            self.blobs[("main",path)] = "s-" + path
            self.blobs[("candidate",path)] = "s-" + path
            self.blobs[(self.merged,path)] = "s-" + path
        for path in gate.INVOICE_REVIEW_PRESERVED:
            self.blobs[("reviewer",path)] = "s-" + path
            self.blobs[("candidate",path)] = "s-" + path
        for path in gate.AR_PRESERVED:
            self.blobs[("receivables",path)] = "s-" + path
            self.blobs[("candidate",path)] = "s-" + path
        for path in gate.RETAINED_PYTHON_TESTS:
            self.sources[("reviewer",path)] = (
                "class Suite:\n"
                "    def test_original(self): pass\n"
                "    def test_rejected(self): pass\n"
            )
            self.sources[("candidate",path)] = (
                "class Suite:\n"
                "    def test_original(self): pass\n"
                "    def test_rejected(self): pass\n"
                "    def test_added(self): pass\n"
            )

    def ancestor(self, main, candidate):
        return self.is_ancestor

    def merge_tree(self, main, candidate):
        return self.merged

    def tree(self, candidate):
        return self.candidate_tree

    def blob(self, ref, path):
        return self.blobs[(ref, path)]

    def source(self, ref, path):
        return self.sources[(ref, path)]


class MainCandidateAuditTests(unittest.TestCase):
    def setUp(self):
        self.git = FakeGit()

    def check(self):
        return gate.audit(self.git, main="main", candidate="candidate",
                          invoice="reviewer", ar="receivables")

    def test_preserves_main_and_both_siblings_but_does_not_authorize_release(self):
        out = self.check()
        self.assertEqual(out["status"], "SOURCE_PRESERVATION_PASS_NOT_RELEASE_APPROVAL")
        self.assertEqual(out["protected_main_assets"], 9)
        self.assertEqual(out["invoice_review_exact_blobs"], 7)
        self.assertEqual(out["receivables_exact_blobs"], 3)
        self.assertEqual(out["original_python_test_names_retained"], 4)
        self.assertFalse(out["release_approved"])
        self.assertFalse(out["oracle_deployed"])
        self.assertFalse(out["main_merged"])

    def test_main_divergence_blocks_previously_reviewed_merge(self):
        self.git.is_ancestor = False
        with self.assertRaisesRegex(gate.GateFailure,"Main has commits"):
            self.check()

    def test_merged_tree_not_expected_candidate_blocks(self):
        self.git.merged = "cd" * 20
        with self.assertRaisesRegex(gate.GateFailure,"Unexpected merge"):
            self.check()

    def test_main_assets_cannot_change_during_merge(self):
        path = gate.PROTECTED_MAIN[1]
        self.git.blobs[("candidate",path)] = "modified"
        with self.assertRaisesRegex(gate.GateFailure,"Protected main asset"):
            self.check()

    def test_sibling_invoice_permission_migration_cannot_silently_change(self):
        path = gate.INVOICE_REVIEW_PRESERVED[0]
        self.git.blobs[("candidate",path)] = "different permissions"
        with self.assertRaisesRegex(gate.GateFailure,"Invoice-review sibling"):
            self.check()

    def test_sibling_ar_data_client_cannot_silently_change(self):
        path = gate.AR_PRESERVED[0]
        self.git.blobs[("candidate",path)] = "mock instead of source"
        with self.assertRaisesRegex(gate.GateFailure,"Receivables sibling"):
            self.check()

    def test_original_postgres_test_cannot_be_dropped(self):
        path = gate.RETAINED_PYTHON_TESTS[0]
        self.git.sources[("candidate",path)] = (
            "class Suite:\n    def test_original(self): pass\n")
        with self.assertRaisesRegex(gate.GateFailure,"regression test removed"):
            self.check()

    def test_bad_python_test_syntax_fails_closed(self):
        path = gate.RETAINED_PYTHON_TESTS[1]
        self.git.sources[("candidate",path)] = "def broken(:"
        with self.assertRaisesRegex(gate.GateFailure,"invalid syntax"):
            self.check()

    def test_untrusted_refs_rejected_before_git_use(self):
        for value in ("--upload-pack", "HEAD:foo", "a..b", "feature//@x",
                      "ref@{1}", "ref\nother", "../secret", ""):
            with self.subTest(value=value),self.assertRaises(gate.GateFailure):
                gate.checked_ref(value)
        self.assertEqual(gate.checked_ref("origin/main"), "origin/main")


if __name__ == "__main__":
    unittest.main()
