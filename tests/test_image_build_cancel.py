"""Operator cancellation of a builder's image builds (ImageManager.cancel_build)."""

import hashlib
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

from sandboxes.build_deadline import BUILD_CANCELLED_ERROR
from sandboxes.images import (
    DockerImageRuntime,
    ImageBuildSpec,
    ImageBuildStore,
    ImageManager,
    ImageStore,
    MaterializedBuildContext,
)
from tests.support import wait_until

TEST_TIER = "contract"


def _context(dockerfile: bytes = b"FROM scratch\n"):
    identity = f"archive:sha256:{hashlib.sha256(dockerfile).hexdigest()}"

    def materialize() -> MaterializedBuildContext:
        temporary = TemporaryDirectory(prefix="test-cancel-context-")
        Path(temporary.name, "Dockerfile").write_bytes(dockerfile)
        return MaterializedBuildContext(Path(temporary.name), temporary, identity)

    return identity, materialize


def _gone(pid: int) -> bool:
    """The build CLI was stopped and reaped (it is this process's child)."""
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return True
    return False


@unittest.skipUnless(os.name == "posix", "owned process groups require POSIX")
class CancelBuildTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        root = self.root

        class Runtime(DockerImageRuntime):
            # "hang-*" builds write their pid and ignore SIGTERM, so a
            # cancellation must stop them through the owned group's SIGKILL.
            def build_command(self, spec, **kwargs):
                if not spec.id.startswith("hang"):
                    return (sys.executable, "-c", "print('built')")
                pid_path = root / f"{spec.id}.pid"
                return (sys.executable, "-c",
                        "import os,signal,time; from pathlib import Path; "
                        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                        f"Path({str(pid_path)!r}).write_text(str(os.getpid())); time.sleep(60)")

        self.manager = ImageManager(
            ImageStore(root / "images.sqlite"), Runtime(),
            max_active_builds=1, queue_builds=True, max_queued_builds=1,
            build_execution_timeout_seconds=120,
        )

    def submit(self, name):
        identity, materialize = _context()
        return self.manager.start_build(
            ImageBuildSpec(id=name, tag=f"fixture/{name}:latest", context_path="."),
            context_identity=identity, materialize_context=materialize,
        )[0]

    def stored(self, build_id):
        return ImageBuildStore(self.root / "images.sqlite").get(build_id)

    def test_running_and_queued_builds_are_cancelled_and_release_their_slot(self):
        running = self.submit("hang-running")
        queued = self.submit("hang-queued")
        pid = int(wait_until(lambda: (self.root / "hang-running.pid").exists()
                             and (self.root / "hang-running.pid").read_text()))

        outcome, record = self.manager.cancel_build(queued.build_id)
        self.assertEqual((outcome, record.status, record.error), ("cancelled", "failed", BUILD_CANCELLED_ERROR))
        self.assertEqual(self.stored(queued.build_id), record)
        self.assertFalse(Path(queued.context_path).exists())  # Its context is cleaned up.

        outcome, record = self.manager.cancel_build(running.build_id)
        self.assertEqual((outcome, record.status, record.error), ("cancelled", "failed", BUILD_CANCELLED_ERROR))
        self.assertEqual(self.stored(running.build_id), record)
        self.assertTrue(_gone(pid))
        self.assertFalse(Path(running.context_path).exists())
        self.assertIsNone(self.manager.get_image("hang-running"))
        self.assertEqual(self.manager.active_build_count(), 0)
        self.assertFalse((self.root / "hang-queued.pid").exists())  # It never ran.

        # A finished build, including a cancelled one, answers with its record.
        self.assertEqual(self.manager.cancel_build(running.build_id), ("finished", record))
        self.assertEqual(self.manager.cancel_build("no-such-build"), ("not_found", None))

        # The slot is free: the next build runs to success.
        following = self.submit("next")
        done = self.manager.wait_for_build(following.build_id, timeout_seconds=10)
        self.assertEqual(done.status, "succeeded")
        self.assertEqual(self.manager.cancel_build(following.build_id), ("finished", done))


if __name__ == "__main__":
    unittest.main()
