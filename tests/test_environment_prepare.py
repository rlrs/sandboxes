import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from sandboxes import environment_prepare as prepare
from sandboxes.commit_policy import CommitBuild, CommitPolicy, CommitRefused
from sandboxes.managed_registry import RegistryClient

TEST_TIER = "contract"


class FakeChild:
    """A preparation child whose ``communicate`` runs a test-supplied action."""

    def __init__(self, action):
        self.action, self.returncode, self.killed, self.reaped = action, 0, False, False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def communicate(self, data, *, timeout):
        self.request = json.loads(data)
        self.action(self)

    def poll(self):
        return None if not self.killed else -9

    def kill(self):
        self.killed = True

    def wait(self):
        self.reaped = self.killed


class CommitPreparationProtocolTests(unittest.TestCase):
    """The parent side of the keyless ``commit-upper`` child protocol."""

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "scratch"
        self.root.mkdir(mode=0o700)
        self.client = RegistryClient("http://registry.example", timeout_seconds=12)
        self.commit = CommitBuild("s1", 3, "c-1", "img-1", "registry.local/managed/base@sha256:" + "1" * 64,
                                  "sha256:" + "2" * 64, "sha256:" + "3" * 64, 123, CommitPolicy.of())
        self.children = []

    def invoke(self, action, *, root=None, timeout_seconds=30):
        def spawn(*args, **kwargs):
            self.children.append(FakeChild(action))
            return self.children[-1]
        with patch.object(prepare.subprocess, "Popen", side_effect=spawn):
            return prepare.prepare_commit_in_subprocess(self.client, self.commit, root or self.root,
                                                        timeout_seconds=timeout_seconds)

    def write_result(self, value):
        path = self.root / prepare.RESULT_NAME
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    def refused(self, child):
        self.write_result({"status": "refused", "error_code": "commit_too_large"})

    def test_parent_symlink_is_canonicalized_but_scratch_symlink_is_rejected(self):
        parent_alias = self.root.parent / "alias"
        parent_alias.symlink_to(self.root.parent, target_is_directory=True)
        with self.assertRaises(CommitRefused):
            self.invoke(self.refused, root=parent_alias / "scratch")
        self.assertEqual(self.children[0].request["root"], str(self.root))
        scratch_alias = self.root.parent / "scratch-alias"
        scratch_alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.invoke(self.refused, root=scratch_alias)
        self.assertEqual(len(self.children), 1)

    def test_private_root_is_checked_before_spawn(self):
        self.root.chmod(0o755)
        with self.assertRaises(ValueError):
            self.invoke(self.refused)
        self.assertEqual(self.children, [])

    def test_timeout_and_interruption_kill_and_reap_the_child(self):
        for failure in (subprocess.TimeoutExpired("private command", 0.01), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                def hang(child, failure=failure):
                    raise failure
                expected = prepare.PreparationError if isinstance(failure, subprocess.TimeoutExpired) else KeyboardInterrupt
                with self.assertRaises(expected) as caught:
                    self.invoke(hang)
                self.assertTrue(self.children[-1].reaped)
                self.assertNotIn("private command", str(caught.exception))

    def test_failed_child_and_error_results_fail_closed(self):
        def exits(child):
            child.returncode = 2
        with self.assertRaises(prepare.PreparationError):
            self.invoke(exits)
        for value in ({"status": "error"}, {"status": "refused", "error_code": "private exception text"},
                      {"status": "ok", "commit": {}}):
            with self.subTest(value=value):
                with self.assertRaises(prepare.PreparationError):
                    self.invoke(lambda child, value=value: self.write_result(value))
                (self.root / prepare.RESULT_NAME).unlink()

    def test_result_symlink_fifo_and_oversize_cannot_be_read_as_response(self):
        for kind in ("symlink", "fifo", "oversize"):
            with self.subTest(kind=kind):
                def communicate(child, kind=kind):
                    result = self.root / prepare.RESULT_NAME
                    if kind == "symlink":
                        result.symlink_to(self.root.parent / "not-a-result")
                    elif kind == "fifo":
                        os.mkfifo(result, 0o600)
                    else:
                        result.write_bytes(b" " * (prepare.MAX_RESULT_BYTES + 1))
                        result.chmod(0o600)
                with self.assertRaises(prepare.PreparationError):
                    self.invoke(communicate)
                (self.root / prepare.RESULT_NAME).unlink()


if __name__ == "__main__":
    unittest.main()
