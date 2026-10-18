from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from sandboxes.resident_memory import ResidentMemorySampler


class ResidentMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.proc = self.root / "proc"
        self.cg = self.root / "cgroup"
        self.container = "a" * 64
        self.path = self.cg / "ucloud-sandboxes" / self.container
        self.path.mkdir(parents=True)
        process = self.proc / "123"
        process.mkdir(parents=True)
        self.stat = process / "stat"
        self.stat.write_text("123 (sentry) S " + "0 " * 18 + "456 0")
        (process / "cgroup").write_text("0::/ucloud-sandboxes/" + self.container)
        (self.path / "memory.current").write_text("1000")
        (self.path / "memory.stat").write_text(
            "shmem 0\nanon 100\nfile 800\nfile_dirty 50\nfile_writeback 20\nworkingset_refault_file 7\n"
        )
        self.sampler = ResidentMemorySampler(cgroup_root=self.cg, proc_root=self.proc)

    def sample(self, **kwargs):
        return self.sampler.sample(
            ("s", 1),
            pid=123,
            start_time_ticks=456,
            container_id=self.container,
            **kwargs,
        )

    def test_containment_is_resolved_once_per_unchanged_cgroup(self):
        expected = "/ucloud-sandboxes/" + self.container
        with patch.object(Path, "resolve", autospec=True, side_effect=Path.resolve) as resolve:
            for _ in range(3):
                self.assertIsNotNone(self.sample(expected_path=expected))
            self.assertEqual(resolve.call_count, 1)
            # A replaced directory is a new identity: prove containment again.
            moved = self.path.with_name("old")
            self.path.rename(moved)
            self.path.mkdir()
            for name in ("memory.current", "memory.stat"):
                (self.path / name).write_bytes((moved / name).read_bytes())
            self.assertIsNotNone(self.sample(expected_path=expected))
            self.assertEqual(resolve.call_count, 2)
            for child in self.path.iterdir():
                child.unlink()
            self.path.rmdir()
            self.path.symlink_to(self.root, target_is_directory=True)
            self.assertIsNone(self.sample(expected_path=expected))

    def test_pid_reuse_invalidates_observation_and_no_configured_limit_fallback(self):
        self.assertIsNotNone(self.sample())
        self.stat.write_text("123 (sentry) S " + "0 " * 18 + "999 0")
        self.assertIsNone(self.sample())
        self.assertIsNone(self.sampler.get(("s", 1)))

    def test_fresh_pre_wait_sample_cannot_price_a_later_heap(self):
        from tests.test_node_runtime import _WakeService
        from sandboxes.background_io import Pressure
        from sandboxes.node_runtime import DirectNodeRuntime
        from sandboxes.warm_park import WarmParkDeferred, WarmParkPolicy

        now = time.monotonic()
        service = _WakeService()
        service.provisioner.registry.relay_wake_fence = lambda *_: False
        service.resident_memory_sample = lambda *_: self.sampler.get(("s", 1))
        runtime = DirectNodeRuntime(service)
        runtime._warm_parks = WarmParkPolicy(lambda: Pressure(.8, 0, 0, 8 * 1024**3))
        key = ('agent', 1, 'request')
        with patch('time.monotonic', return_value=now):
            self.assertIsNotNone(self.sample())
        with patch('time.monotonic', return_value=now + .1):
            with self.assertRaises(WarmParkDeferred):
                runtime.park_with_activity_revision(
                    'agent', generation=1, relay_request_id='request',
                    operation_id='park:request',
                )
            self.assertIsNotNone(self.sampler.get(("s", 1)))  # Cache still fresh.
            self.assertIsNone(runtime._resident_wait_memory_sample(key))
        with patch('time.monotonic', return_value=now + .2):
            (self.path / 'memory.current').write_text(str(1536 * 1024**2))
            self.sample()
            self.assertEqual(
                runtime._resident_wait_memory_sample(key).current_bytes,
                1536 * 1024**2,
            )

    def test_wrong_cgroup_and_incomplete_counters_stay_unknown(self):
        self.assertIsNone(self.sample(expected_path="/another/runtime"))
        (self.path / "memory.stat").write_text("shmem 0\nanon 100\nfile 800\n")
        self.assertIsNone(self.sample())
        self.sampler.retain(())
        self.assertIsNone(self.sampler.get(("s", 1)))

    def test_shared_parent_cgroup_cannot_be_misattributed_to_incarnation(self):
        (self.proc / "123" / "cgroup").write_text("0::/ucloud-sandboxes")
        self.assertIsNone(self.sample())

    def test_tmpfs_pages_are_not_counted_as_reclaimable_without_swap(self):
        counters = self.path / "memory.stat"
        counters.write_text(counters.read_text().replace("shmem 0", "shmem 800"))
        sample = self.sample()
        self.assertEqual(sample.clean_file_bytes, 0)
        from sandboxes.resident_memory import ResidentMemoryReclaimer

        report = ResidentMemoryReclaimer(self.sampler).reclaim(
            ("s", 1), sample, target_bytes=900, is_current=lambda: True
        )
        self.assertEqual(report.reason, "no_reclaimable_cache")
        self.assertEqual(report.requested_bytes, 0)

    def test_unsupported_reclaim_does_not_fall_back_to_anonymous_swap(self):
        import errno
        from sandboxes.resident_memory import ResidentMemoryReclaimer

        (self.path / "memory.reclaim").touch()
        with patch(
            "os.write", side_effect=OSError(errno.EINVAL, "unsupported")
        ) as write:
            report = ResidentMemoryReclaimer(self.sampler).reclaim(
                ("s", 1), self.sample(), target_bytes=500, is_current=lambda: True
            )
        self.assertEqual(write.call_count, 1)
        self.assertEqual(report.reason, "unsupported")
        self.assertEqual(report.reclaimed_bytes, 0)

    def test_cgroup_replacement_cannot_receive_reclaim(self):
        from sandboxes.resident_memory import ResidentMemoryReclaimer

        sample = self.sample()
        self.path.rename(self.path.with_name("old"))
        self.path.mkdir()
        (self.path / "memory.reclaim").touch()
        with patch("os.write") as write:
            report = ResidentMemoryReclaimer(self.sampler).reclaim(
                ("s", 1), sample, target_bytes=500, is_current=lambda: True
            )
        write.assert_not_called()
        self.assertEqual(report.reason, "cgroup_changed")
