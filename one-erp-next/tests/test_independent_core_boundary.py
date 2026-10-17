"""Guard ONE ERP NEXT against accidental legacy/Odoo dependency imports.

This is a source-control boundary, NOT a software supply-chain security audit.
Documents may mention legacy products in comparisons; executable core may not
import their libraries. Third-party dependencies still require license/SBOM scan.
"""
from __future__ import annotations

import ast
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "one_erp_core"
FORBIDDEN = {"odoo", "openerp", "odoolib", "odoorpc"}
DEPENDENCY = re.compile(r"(?i)(?<![\w-])(?:odoo|openerp|odoolib|odoorpc|odoo-rpc-client)(?![\w-])")


def imported_roots(code: str) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(code)):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0].lower() for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0].lower())
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
            func = node.func
            dynamic = (
                isinstance(func, ast.Name) and func.id == "__import__"
            ) or (
                isinstance(func, ast.Attribute) and func.attr == "import_module"
            )
            if dynamic and isinstance(node.args[0].value, str):
                roots.add(node.args[0].value.split(".")[0].lower())
    return roots


class IndependentCoreBoundaryTests(unittest.TestCase):
    def test_all_core_python_imports_exclude_legacy_products(self):
        sources = list(PACKAGE.rglob("*.py"))
        self.assertTrue(sources, "Cannot validate an empty ONE ERP core")
        for path in sources:
            with self.subTest(path=path.relative_to(ROOT)):
                roots = imported_roots(path.read_text(encoding="utf-8"))
                self.assertFalse(
                    roots & FORBIDDEN,
                    f"Legacy framework import in {path.relative_to(ROOT)}: {roots & FORBIDDEN}",
                )

    def test_dependency_manifests_exclude_legacy_framework(self):
        manifests = [ROOT / "pyproject.toml"]
        manifests.extend(ROOT.rglob("requirements*.txt"))
        manifests.extend(ROOT.rglob("package.json"))
        for path in manifests:
            if path.exists():
                with self.subTest(path=path.relative_to(ROOT)):
                    self.assertIsNone(DEPENDENCY.search(path.read_text(encoding="utf-8")))

    def test_guard_detects_direct_and_dynamic_import(self):
        for code in (
            "import odoo.addons",
            "from openerp import models",
            "import importlib\nimportlib.import_module('odoorpc.client')",
            "__import__('odoolib')",
        ):
            with self.subTest(code=code):
                self.assertTrue(imported_roots(code) & FORBIDDEN)


if __name__ == "__main__":
    unittest.main()
