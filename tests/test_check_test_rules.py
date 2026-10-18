"""scripts/check_test_rules.py: which test patterns count, and how the baseline ratchets."""

import unittest

from scripts.check_test_rules import compare, count_violations, lowered


class CountTests(unittest.TestCase):
    def test_counts_each_forbidden_pattern(self):
        source = """
import time
from time import monotonic
from tests.test_routing import fixture
from unittest import mock

def test_case(self):
    time.sleep(0.1)
    started = monotonic()
    handler._select(1)
    mock.patch.object(handler, "_proxy", None)
    mock.patch("sandboxes.cli._client")
    Handler.__new__(Handler)
"""
        self.assertEqual(count_violations(source), {
            "test_import": [4], "sleep": [8], "clock": [9], "private": [10],
            "private_patch": [11, 12], "new": [13],
        })

    def test_allows_own_members_dunders_and_public_underscore_apis(self):
        source = """
class Case:
    def test(self):
        self._helper()
        super()._setup()
        row._replace(state="x")
        os._exit(0)
        value.__class__
        mock.patch.object(store, "insert")
"""
        self.assertEqual(count_violations(source), {})

    def test_strings_and_comments_do_not_count(self):
        self.assertEqual(count_violations('"time.sleep(1) handler._x"  # obj._y\n'), {})


class RatchetTests(unittest.TestCase):
    def test_a_rise_fails_and_a_new_module_starts_at_zero(self):
        failures, _ = compare({"test_a.py": {"sleep": [3, 9]}, "test_new.py": {"private": [5]}},
                              {"test_a.py": {"sleep": 1}})
        self.assertEqual([failure.split(":")[0] for failure in failures], ["tests/test_a.py", "tests/test_new.py"])

    def test_staying_at_or_below_the_baseline_passes_and_reports_improvements(self):
        failures, improvements = compare({"test_a.py": {"sleep": [3]}}, {"test_a.py": {"sleep": 2, "new": 1}})
        self.assertEqual(failures, [])
        self.assertEqual(improvements, ["tests/test_a.py: new", "tests/test_a.py: sleep"])

    def test_update_only_lowers_counts_and_drops_cleared_modules(self):
        baseline = {"test_a.py": {"sleep": 2, "new": 1}, "test_gone.py": {"private": 4}}
        found = {"test_a.py": {"sleep": [1, 2, 3]}, "test_b.py": {"clock": [1]}}
        self.assertEqual(lowered(found, baseline), {"test_a.py": {"sleep": 2}})
