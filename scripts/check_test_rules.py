#!/usr/bin/env python3
"""Hold the line on AGENTS.md's test rules: no test module may gain violations.

Counts, in every tests/test_*.py module, the patterns the rules forbid and
compares them with tests/test_rules_baseline.json. A module fails when a count
rises above its baseline; a module missing from the baseline starts at zero.
After removing violations, run with --update to lower the baseline. --update
never raises a count: accepting a new violation means editing the file by hand,
in review.
"""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
TESTS = REPO / "tests"
BASELINE = TESTS / "test_rules_baseline.json"

RULES = {
    "sleep": "sleeps to synchronize; wait for the end state with tests.support.wait_until",
    "clock": "reads the clock; test time-dependent logic with an explicit `now`",
    "private": "touches another object's private member",
    "private_patch": "patches a private name",
    "new": "builds an object with __new__, skipping its constructor",
    "test_import": "imports another test module; share fixtures through tests/support.py or tests/harness",
}

_CLOCKS = {"monotonic", "monotonic_ns", "perf_counter", "perf_counter_ns", "time", "time_ns"}
# Public APIs that happen to start with an underscore.
_PUBLIC_UNDERSCORE = {"_asdict", "_current_frames", "_exit", "_fields", "_getframe", "_make", "_replace"}


def _private(name: str) -> bool:
    return name.startswith("_") and not name.endswith("__") and name not in _PUBLIC_UNDERSCORE


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return ""


class _Visitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.found: dict[str, list[int]] = {rule: [] for rule in RULES}
        # Bare names bound to time.sleep, asyncio.sleep or a clock by `from x import y`.
        self.imported: dict[str, str] = {}

    def _hit(self, rule: str, node: ast.AST) -> None:
        self.found[rule].append(node.lineno)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if module.startswith("tests.test_") or (module == "tests" and any(a.name.startswith("test_") for a in node.names)):
            self._hit("test_import", node)
        if module in {"time", "asyncio"}:
            for alias in node.names:
                if alias.name == "sleep" or (module == "time" and alias.name in _CLOCKS):
                    self.imported[alias.asname or alias.name] = alias.name
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        if any(alias.name.startswith("tests.test_") for alias in node.names):
            self._hit("test_import", node)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = _dotted(node.func)
        bare = self.imported.get(name)
        if name in {"time.sleep", "asyncio.sleep"} or bare == "sleep":
            self._hit("sleep", node)
        elif (name.startswith("time.") and name[5:] in _CLOCKS) or (bare in _CLOCKS):
            self._hit("clock", node)
        elif name.endswith("__new__"):
            self._hit("new", node)
        last = name.rsplit(".", 1)[-1]
        strings = [arg.value for arg in node.args if isinstance(arg, ast.Constant) and isinstance(arg.value, str)]
        if last == "object" and name.endswith("patch.object") and len(node.args) > 1 and strings and _private(strings[0]):
            self._hit("private_patch", node)
        elif last == "patch" and strings and _private(strings[0].rsplit(".", 1)[-1]):
            self._hit("private_patch", node)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        owner = node.value
        own = isinstance(owner, ast.Name) and owner.id in {"self", "cls"}
        via_super = isinstance(owner, ast.Call) and _dotted(owner.func) == "super"
        if _private(node.attr) and not own and not via_super:
            self._hit("private", node)
        self.generic_visit(node)


def count_violations(source: str) -> dict[str, list[int]]:
    """Line numbers of each rule's violations in one test module's source."""
    visitor = _Visitor()
    visitor.visit(ast.parse(source))
    return {rule: lines for rule, lines in visitor.found.items() if lines}


def scan(tests: Path = TESTS) -> dict[str, dict[str, list[int]]]:
    found = {}
    for path in sorted(tests.glob("test_*.py")):
        violations = count_violations(path.read_text(encoding="utf-8"))
        if violations:
            found[path.name] = violations
    return found


def compare(found: dict[str, dict[str, list[int]]], baseline: dict[str, dict[str, int]]) -> tuple[list[str], list[str]]:
    """Return (failures, improvements) of the current counts against the baseline."""
    failures, improvements = [], []
    for module, rules in sorted(found.items()):
        allowed = baseline.get(module, {})
        for rule, lines in sorted(rules.items()):
            if len(lines) > allowed.get(rule, 0):
                shown = ", ".join(map(str, lines[:12])) + (", ..." if len(lines) > 12 else "")
                failures.append(f"tests/{module}: {len(lines)} > {allowed.get(rule, 0)} allowed; {RULES[rule]} "
                                f"(lines {shown})")
    for module, rules in sorted(baseline.items()):
        for rule, count in sorted(rules.items()):
            if len(found.get(module, {}).get(rule, [])) < count:
                improvements.append(f"tests/{module}: {rule}")
    return failures, improvements


def lowered(found: dict[str, dict[str, list[int]]], baseline: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
    """The baseline with every count lowered to the current one, never raised."""
    result = {}
    for module, rules in baseline.items():
        kept = {rule: min(count, len(found.get(module, {}).get(rule, []))) for rule, count in rules.items()}
        kept = {rule: count for rule, count in kept.items() if count}
        if kept:
            result[module] = dict(sorted(kept.items()))
    return dict(sorted(result.items()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--update", action="store_true", help="lower the baseline to the current counts")
    args = parser.parse_args(argv)
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    found = scan()
    if args.update:
        BASELINE.write_text(json.dumps(lowered(found, baseline), indent=1) + "\n", encoding="utf-8")
        return 0
    failures, improvements = compare(found, baseline)
    for failure in failures:
        print(failure, file=sys.stderr)
    if improvements:
        print(f"{len(improvements)} counts dropped below the baseline; "
              "run scripts/check_test_rules.py --update to lock them in.", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
