#!/usr/bin/env python3
"""Offline Ask ONE checks; does not require Odoo or connect to company data."""
from __future__ import annotations

import ast
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

SOURCE = Path(__file__).resolve().parents[1] / "one_custom/one_ui/models/ask_one.py"
tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
klass = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "OneAskWizard")


def isolated_method(name, namespace):
    method = next(
        node for node in klass.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )
    module = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
    exec(compile(module, str(SOURCE), "exec"), namespace)
    return namespace[name]


class NoAccess(Exception):
    pass


def require(condition, reason):
    if not condition:
        raise AssertionError(reason)


finance_access = isolated_method(
    "_require_finance_access",
    {"AccessError": NoAccess, "_": lambda message: message},
)


class TestUser:
    def __init__(self, allowed):
        self.allowed = allowed

    def has_group(self, group):
        return self.allowed and group == "account.group_account_user"


for allowed in (False, True):
    context = SimpleNamespace(env=SimpleNamespace(user=TestUser(allowed)))
    try:
        finance_access(context)
        denied = False
    except NoAccess:
        denied = True
    require(denied == (not allowed), "Finance authorization guard failed")


class TestAsk:
    def __init__(self):
        self.ranges = []

    def _contains_any(self, question, terms):
        return any(term in question for term in terms)

    def _period_metrics(self, start, end):
        self.ranges.append((start, end))
        if start.month == 10:
            return {"revenue": 100, "profit": 25}
        return {"revenue": 80, "profit": 12}

    def _money(self, amount):
        return f"{amount:.2f} SAR"


compare = isolated_method(
    "_financial_period_answer",
    {
        "timedelta": timedelta,
        "fields": SimpleNamespace(Date=SimpleNamespace(context_today=lambda _self: date(2026, 10, 12))),
    },
)
ask = TestAsk()
response = compare(ask, "قارن مبيعات الشهر بالشهر السابق", "revenue", True)
require("2026-10-01" in response and "2026-10-12" in response, "Current MTD dates missing")
require("2026-09-01" in response and "2026-09-12" in response, "Previous MTD dates missing")
require("+25.0%" in response, "Revenue percentage comparison incorrect")
require(ask.ranges == [(date(2026, 10, 1), date(2026, 10, 12)), (date(2026, 9, 1), date(2026, 9, 12))], "Comparison incorrectly uses a full preceding month")

ask.ranges.clear()
response = compare(ask, "مبيعات الشهر اللي فات", "revenue", True)
require(ask.ranges == [(date(2026, 9, 1), date(2026, 9, 30))], "Full preceding month selection failed")

ask.ranges.clear()
response = compare(ask, "profit this month", "profit", False)
require("25.00 SAR" in response, "Profit calculation selection failed")
require(ask.ranges == [(date(2026, 10, 1), date(2026, 10, 12))], "Current month selection failed")

source = SOURCE.read_text(encoding="utf-8")
answer_start = source.index("    def _answer_question(")
answer_head = source[answer_start:source.index("        if self._contains_any(q, (", answer_start)]
require("one_get_pulse" not in answer_head, "Unconditional dashboard financial data read reintroduced")
require(source.count("self._require_finance_access()") >= 6, "Sensitive finance branches are not access-checked")
require("sudo()" not in source, "Unexpected Odoo sudo() in Ask ONE")

print("PASS: Ask ONE finance authorization and MTD comparison unit checks")
print("NOTE: Live Odoo ORM, access rules and Oracle deployment still require integration tests.")
