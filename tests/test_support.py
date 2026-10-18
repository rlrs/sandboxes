"""tests/support.py helpers that other tests rely on."""

import threading
import unittest

from tests.support import wait_until


class WaitUntilTests(unittest.TestCase):
    def test_returns_the_first_truthy_value(self):
        values = iter([None, 0, "", "done"])
        self.assertEqual(wait_until(lambda: next(values)), "done")

    def test_sees_state_written_by_another_thread(self):
        state = {}
        writer = threading.Timer(0.01, state.update, kwargs={"row": "final"})
        writer.start()
        self.addCleanup(writer.cancel)
        self.assertEqual(wait_until(lambda: state.get("row")), "final")

    def test_fails_with_the_given_message_when_the_state_never_holds(self):
        with self.assertRaisesRegex(AssertionError, "row never written"):
            wait_until(lambda: False, timeout=0.01, message="row never written")
