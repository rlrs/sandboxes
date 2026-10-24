"""C1.1 pause tier: Warden pause/thaw, crash windows, activity thaws, policy."""

from contextlib import contextmanager
from dataclasses import replace
import errno
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests import test_direct_provisioner as provisioner_fixtures
from tests import test_direct_warden as warden_fixtures
from sandboxes import pause_tier
from sandboxes.direct_service import DirectSandboxService
from sandboxes.direct_warden import CommandResult, DirectRunscWarden, DirectWardenError
from sandboxes.hibernation import HibernationState
from sandboxes.node_agent import NodeAgentHandler
from sandboxes.node_runtime import DirectNodeRuntime
from sandboxes.pause_tier import (
    PREFETCH_MIN_SWAP_BYTES,
    TRANSFER_BREAK_EVEN_SECONDS,
    PauseStats,
    cgroup_swap_bytes,
    memory_pieces,
    park_tier,
    prefetch,
    ZSWAP_SHARE_OF_BOUND,
    cap_zswap,
)
from sandboxes.sandbox import WarmParkDeferred

GIB = 1024**3
MIB = 1024**2
INF = float("inf")


class PausePolicyTests(unittest.TestCase):
    def test_explicit_parks_hibernate_and_idle_or_unhinted_waits_pause(self):
        self.assertEqual(park_tier("explicit"), "hibernate")
        self.assertEqual(park_tier("idle", expected_wait_seconds=1e9,
                                   resident_bytes=GIB, footprint_bytes=GIB), "pause")
        self.assertEqual(park_tier("relay", resident_bytes=GIB, footprint_bytes=GIB), "pause")
        with self.assertRaises(ValueError):
            park_tier("drain")

    def test_zswap_holds_at_most_a_fixed_share_of_a_paused_cgroups_bound(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "proc/7").mkdir(parents=True)
            (root / "proc/7/cgroup").write_text("0::/ucloud-sandboxes/abc\n")
            cgroup = root / "cgroup/ucloud-sandboxes/abc"
            cgroup.mkdir(parents=True)
            (cgroup / "memory.max").write_text(f"{2 * GIB}\n")
            (cgroup / "memory.zswap.max").write_text("max\n")
            enabled = root / "enabled"

            def cap():
                return cap_zswap(7, proc_root=root / "proc", cgroup_root=root / "cgroup", zswap_enabled=enabled)

            enabled.write_text("N\n")
            self.assertIsNone(cap())  # zswap off: swap only, nothing to bound.
            enabled.write_text("Y\n")
            self.assertEqual(cap(), int(2 * GIB * ZSWAP_SHARE_OF_BOUND))
            self.assertEqual((cgroup / "memory.zswap.max").read_text(), str(int(2 * GIB * ZSWAP_SHARE_OF_BOUND)))
            self.assertIsNone(cap())  # Set once; a later pause leaves it.
            (cgroup / "memory.zswap.max").write_text("max\n")
            (cgroup / "memory.max").write_text("max\n")
            self.assertIsNone(cap())  # No bound to take a share of.


class ThawPrefetchTests(unittest.TestCase):
    """Bounded parallel prefetch of a fake memory file (qualification: 0.83 s
    for 645 MiB over 8 readers, against 3.6-5.0 s of guest refaults)."""

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "application_memory.active"
        # Data, an 8 MiB hole (never written), data, then a trailing hole.
        with self.path.open("wb") as memory:
            memory.write(os.urandom(8 * MIB))
            memory.seek(16 * MIB)
            memory.write(os.urandom(8 * MIB))
            memory.truncate(32 * MIB)
        self.fd = os.open(self.path, os.O_RDONLY)
        self.addCleanup(os.close, self.fd)
        self.slots = threading.BoundedSemaphore(pause_tier.PREFETCH_NODE_THREADS)

    def _slow_reads(self, seconds, *, active=None, fail_at=None):
        """A swap device: each 1 MiB read blocks for `seconds`."""
        real, calls, guard = os.preadv, [], threading.Lock()

        def preadv(fd, buffers, offset):
            with guard:
                calls.append(offset)
                if fail_at is not None and len(calls) == fail_at:
                    raise OSError(errno.EIO, "injected swap read failure")
                if active is not None:  # Reads in flight, and reader threads alive.
                    active[0] += 1
                    active[1] = max(active[1], active[0])
                    active[2] = max(active[2], sum(
                        item.name == "thaw-prefetch" for item in threading.enumerate()))
            time.sleep(seconds)
            try:
                return real(fd, buffers, offset)
            finally:
                if active is not None:
                    with guard:
                        active[0] -= 1
        return patch.object(pause_tier.os, "preadv", side_effect=preadv), calls

    def test_node_slots_bound_readers_across_concurrent_thaws(self):
        active = [0, 0, 0]
        reads, calls = self._slow_reads(0.005, active=active)
        slots = threading.BoundedSemaphore(2)
        with reads:
            thaws = [threading.Thread(target=prefetch, args=(self.fd, memory_pieces(self.fd)),
                                      kwargs={"slots": slots, "cancelled": lambda: False})
                     for _ in range(6)]
            for thaw in thaws:
                thaw.start()
            for thaw in thaws:
                thaw.join()
        # Waiting thaws hold neither a thread nor a 1 MiB buffer; every thaw reads.
        self.assertEqual(active[1:], [2, 2])
        self.assertEqual(len(calls), 6 * 16)

    def test_a_failed_read_stops_the_readers_and_raises_after_they_exit(self):
        reads, calls = self._slow_reads(0.005, fail_at=3)
        with reads, self.assertRaises(OSError):
            prefetch(self.fd, memory_pieces(self.fd), slots=self.slots, cancelled=lambda: False)
        self.assertLess(len(calls), 16)
        self.assertFalse([item for item in threading.enumerate() if item.name == "thaw-prefetch"])
        self.assertEqual(self.slots._value, pause_tier.PREFETCH_NODE_THREADS)  # Every slot returned.

    def test_readers_the_node_cannot_start_are_skipped(self):
        real, started = threading.Thread.start, []

        def start(thread):
            if thread.name == "thaw-prefetch" and len(started) == 2:
                raise RuntimeError("can't start new thread")
            started.append(thread)
            real(thread)

        with patch.object(threading.Thread, "start", start):
            read = prefetch(self.fd, memory_pieces(self.fd), slots=self.slots, cancelled=lambda: False)
        self.assertEqual((read, len(started)), (16 * MIB, 2))
        with patch.object(threading.Thread, "start", side_effect=RuntimeError("no threads")):
            self.assertEqual(prefetch(self.fd, memory_pieces(self.fd), slots=self.slots,
                                      cancelled=lambda: False), 0)

    def test_swap_is_read_from_the_process_unified_cgroup(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "proc/7").mkdir(parents=True)
            (root / "cg/sandboxes/a").mkdir(parents=True)
            (root / "cg/sandboxes/a/memory.swap.current").write_text("123\n")

            def swap(membership):
                (root / "proc/7/cgroup").write_text(membership)
                return cgroup_swap_bytes(7, proc_root=root / "proc", cgroup_root=root / "cg")

            self.assertEqual(swap("1:name=systemd:/x\n0::/sandboxes/a\n"), 123)
            self.assertIsNone(swap("0::/sandboxes/../a\n"))
            self.assertIsNone(swap("0::/sandboxes/b\n"))
            self.assertIsNone(swap("0::/sandboxes/a\n0::/sandboxes/a\n"))
            self.assertIsNone(cgroup_swap_bytes(8, proc_root=root / "proc", cgroup_root=root / "cg"))


class PauseWardenTests(unittest.TestCase):
    def setUp(self):
        warden_fixtures.DirectRunscWardenTests.setUp(self)
        self.warden = self._warden()

    def tearDown(self):
        self.temporary.cleanup()

    def _warden(self):
        # A new instance models a node-agent restart over the same roots.
        return DirectRunscWarden(self.config, runner=self.runner, fencer=self.fencer,
                                 storage=self.storage, rootfs_lifecycle=self.rootfs)

    @property
    def key(self):
        return (self.sandbox.sandbox_id, self.sandbox.sandbox_generation)

    def _paused(self):
        return self.warden.is_paused(*self.key)

    def test_pause_keeps_live_authority_and_exec_thaws_first(self):
        running = self.warden.create(self.sandbox, operation_id="create:1")
        with patch.object(pause_tier, "cap_zswap") as cap:
            self.assertTrue(self.warden.pause(self.sandbox))
        cap.assert_called_once_with(running.sentry_pid, proc_root=self.warden.config.proc_root)
        self.assertEqual(self.runner.status, "paused")
        self.assertTrue(self._paused())
        # No ownership change: the journal revision and live identity are intact.
        self.assertEqual(self.warden.inspect(self.sandbox), running)
        observed = []
        with self.warden.exec_lease(self.sandbox, ("/bin/true",)) as command:
            observed.append(self.runner.status)
        self.assertEqual(observed, ["running"])
        self.assertIn("exec", command)
        self.assertFalse(self._paused())
        self.assertIsNone(self.warden.thaw(self.sandbox))  # Fast path: one lstat.
        stats = self.warden.pause_stats.snapshot()
        self.assertEqual((stats["pauses"], stats["thaws"]), (1, 1))

    def test_pause_requires_a_running_journal(self):
        self.assertFalse(self.warden.pause(self.sandbox))  # No journal yet.
        self.warden.create(self.sandbox, operation_id="create:1")
        self.warden.park(self.sandbox, operation_id="park:1")
        self.assertFalse(self.warden.pause(self.sandbox))
        self.assertFalse(self._paused())


class ThawPrefetchWardenTests(unittest.TestCase):
    setUp = PauseWardenTests.setUp
    tearDown = PauseWardenTests.tearDown
    _warden = PauseWardenTests._warden
    key = PauseWardenTests.key
    _paused = PauseWardenTests._paused

    def _ram(self, *, swap=64 * MIB):
        """RAM-mode memory: 8 MiB of tmpfs-like data, `swap` bytes swapped."""
        ram = (self.root / "ram").resolve()
        ram.mkdir(mode=0o700)
        self.config = replace(self.config, application_memory_root=ram)
        self.warden = self._warden()
        self.warden.create(self.sandbox, operation_id="create:1")
        self.memory = ram / self.memory_directory / "application_memory.active"
        self.memory.write_bytes(os.urandom(8 * MIB))
        self.memory.chmod(0o600)
        swapped = patch.object(pause_tier, "cgroup_swap_bytes", return_value=swap)
        swapped.start()
        self.addCleanup(swapped.stop)
        self.warden.pause(self.sandbox)

    def _stats(self, warden=None):
        stats = (warden or self.warden).pause_stats.snapshot()
        return stats["thaw_prefetches"], stats["thaw_prefetched_bytes"]

    def test_activity_reads_swapped_memory_back_before_resume(self):
        self._ram()
        statuses, real = [], pause_tier.prefetch

        def observe(*args, **kwargs):  # Reclaim sees the thaw before the marker goes.
            statuses.append((self.runner.status, self._paused(), self.warden.thawing(*self.key)))
            return real(*args, **kwargs)

        with patch.object(pause_tier, "prefetch", side_effect=observe):
            with self.warden.exec_lease(self.sandbox, ("/bin/true",)):
                statuses.append((self.runner.status, self._paused(), self.warden.thawing(*self.key)))
        self.assertEqual(statuses, [("paused", True, True), ("running", False, False)])
        self.assertEqual(self._stats(), (1, 8 * MIB))
        self.assertEqual(self.warden._prefetches, {})

    def test_no_prefetch_without_swap_for_file_memory_reads_or_a_capture(self):
        self._ram()
        swapped = pause_tier.cgroup_swap_bytes  # The patch _ram installed.
        with patch.object(pause_tier, "prefetch") as prefetched:
            for swap in (None, 0, PREFETCH_MIN_SWAP_BYTES - 1):
                with self.subTest(swap=swap):  # No reclaim moved anything out.
                    swapped.return_value = swap
                    self.warden.exec(self.sandbox, ("/bin/true",))
                    self.assertEqual(self.runner.status, "running")
                    self.warden.pause(self.sandbox)
            swapped.return_value = GIB
            with self.warden.exec_lease(self.sandbox, ("ctl",), keep_paused=True):
                pass  # A status read keeps the pause and its swap.
            self.assertEqual(self.runner.status, "paused")
            parked = self.warden.park(self.sandbox, operation_id="park:1")
            self.assertEqual(parked.state, HibernationState.PARKED)
            self.warden.resume(self.sandbox, operation_id="wake:1")
            real_run = self.runner.run  # A failed pause rolls back to a running guest.
            with patch.object(self.runner, "run", side_effect=lambda argv, *, timeout: (
                    CommandResult(tuple(argv), 1, stderr="injected") if "pause" in argv
                    else real_run(argv, timeout=timeout))), self.assertRaises(DirectWardenError):
                self.warden.pause(self.sandbox)
            self.warden.config = replace(self.config, application_memory_root=None)
            self.warden.pause(self.sandbox)  # File memory: reads would charge the agent.
            self.warden.exec(self.sandbox, ("/bin/true",))
        prefetched.assert_not_called()

    def test_failed_slow_or_foreign_prefetch_still_resumes(self):
        self._ram()
        with patch.object(pause_tier, "prefetch", side_effect=OSError(errno.EIO, "swap")), \
                self.assertLogs("sandboxes.direct_warden", "WARNING"):
            self.warden.exec(self.sandbox, ("/bin/true",))
        self.assertEqual((self.runner.status, self._paused(), self._stats()), ("running", False, (0, 0)))
        real = os.preadv

        def slow(fd, buffers, offset):
            time.sleep(0.1)
            return real(fd, buffers, offset)

        self.warden.pause(self.sandbox)
        with patch.object(pause_tier, "PREFETCH_SECONDS", 0.05), \
                patch.object(pause_tier.os, "preadv", side_effect=slow):
            started = time.monotonic()
            self.assertIsNotNone(self.warden.thaw(self.sandbox))
        self.assertLess(time.monotonic() - started, 1.0)
        prefetches, read = self._stats()
        self.assertEqual(prefetches, 1)
        self.assertLessEqual(read, 2 * MIB)  # At most one read per reader, not four.
        self.memory.chmod(0o644)  # Only ever a privately owned memory file.
        self.warden.pause(self.sandbox)
        self.warden.exec(self.sandbox, ("/bin/true",))
        self.assertEqual((self.runner.status, self._stats()[0]), ("running", 1))

    def test_delete_cancels_an_inflight_prefetch(self):
        self._ram()
        reading, real = threading.Event(), os.preadv

        def slow(fd, buffers, offset):
            reading.set()
            time.sleep(0.5)
            return real(fd, buffers, offset)

        with patch.object(pause_tier, "PREFETCH_SECONDS", 60.0), \
                patch.object(pause_tier.os, "preadv", side_effect=slow):
            thaw = threading.Thread(target=self.warden.thaw, args=(self.sandbox,))
            thaw.start()
            self.assertTrue(reading.wait(5))
            started = time.monotonic()
            self.warden.delete(self.sandbox)  # Uncancelled: 4 reads of 0.5 s per reader.
            elapsed = time.monotonic() - started
            thaw.join(5)
        self.assertLess(elapsed, 1.5)
        self.assertLess(self._stats()[1], 8 * MIB)
        self.assertEqual((self.warden.paused_keys(), self.warden._prefetches), ([], {}))

    def test_crash_during_prefetch_leaves_it_paused_and_a_restart_prefetches(self):
        self._ram()
        with patch.object(pause_tier, "prefetch", side_effect=SystemExit("node agent killed")):
            with self.assertRaises(SystemExit):
                with self.warden.exec_lease(self.sandbox, ("/bin/true",)):
                    self.fail("exec reached a runtime that never resumed")
        self.assertEqual((self.runner.status, self._paused()), ("paused", True))
        restarted = self._warden()
        self.assertEqual(restarted.reconcile(self.sandbox).state, HibernationState.RUNNING)
        self.assertEqual(restarted.exec(self.sandbox, ("/bin/true",)).returncode, 0)
        self.assertEqual((self.runner.status, self._stats(restarted)), ("running", (1, 8 * MIB)))


class _PausingWarden(provisioner_fixtures.FakeWarden):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.paused = set()
        self.prefetching = set()
        self.events = []
        self.pause_stats = PauseStats()

    def pause(self, sandbox):
        self.events.append("pause")
        self.paused.add(self.key(sandbox))
        return True

    def thaw(self, sandbox):
        if self.key(sandbox) not in self.paused:
            return None
        self.events.append("thaw")
        self.paused.discard(self.key(sandbox))
        return 1.5

    def is_paused(self, sandbox_id, generation):
        return (sandbox_id, generation) in self.paused

    def thawing(self, sandbox_id, generation):
        return (sandbox_id, generation) in self.prefetching

    def paused_keys(self):
        return sorted(self.paused)

    @contextmanager
    def exec_lease(self, sandbox, argv, *, keep_paused=False, **kwargs):
        thawed = self.thaw(sandbox)  # As the real lease does, under its lock.
        self.events.append("exec")
        with super().exec_lease(sandbox, argv, **kwargs) as command:
            yield command
        if keep_paused and thawed is not None:
            self.pause(sandbox)

    def park(self, sandbox, *, operation_id):
        self.events.append("hibernate")
        return super().park(sandbox, operation_id=operation_id)


class PauseActivityTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name).resolve()
        fixture = provisioner_fixtures.DirectProvisionerTests()
        with patch.object(provisioner_fixtures, "FakeWarden", _PausingWarden):
            provisioner, _registry, _, _, self.warden = fixture.make(root)
        self.service = DirectSandboxService(
            provisioner, process_runner=provisioner_fixtures.FakeProcessRunner())
        self.created = fixture.create(self.service, fixture.spec())
        self.sandbox_id = self.created.spec.id
        self.key = (self.sandbox_id, self.created.generation)

    def test_heartbeat_footprint_counts_swapped_out_pages(self):
        sample = SimpleNamespace(current_bytes=GIB, swap_bytes=3 * GIB, sampled_at=time.monotonic())
        handler = SimpleNamespace(manager=SimpleNamespace(service=SimpleNamespace(
            cached_resident_memory_sample=lambda *_: sample)))
        entry = NodeAgentHandler._sandbox_inventory_entry(handler, self.created)
        self.assertEqual(entry.memory_observation.memory_bytes, 4 * GIB)


class PauseNodeRuntimeTests(unittest.TestCase):
    def _runtime(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        warden = SimpleNamespace(config=SimpleNamespace(runtime_root=root / "runsc"), paused=set())
        registry = SimpleNamespace(
            path=root / "registry.sqlite",
            load_drain=lambda: SimpleNamespace(draining=False),
            relay_wake_fence=Mock(return_value=False))
        parks = []

        def park(sandbox_id, *, operation_id, background, pause):
            parks.append((operation_id.split(":")[0], pause))
            if pause:
                warden.paused.add((sandbox_id, 1))
            return SimpleNamespace(state="running" if pause else "parked")

        service = SimpleNamespace(
            warden=warden, provisioner=SimpleNamespace(registry=registry),
            open_admission=lambda: None, park=park,
            resident_memory_sample=lambda *_: SimpleNamespace(current_bytes=GIB, swap_bytes=0),
            advance_lifecycle_activity_revision=lambda: 7,
        )
        return DirectNodeRuntime(service), service, parks

    def test_refused_capture_of_a_long_relay_wait_pauses_instead(self):
        runtime, service, parks = self._runtime()
        now = time.time()
        long_hint = {"phase": "model_wait", "evaluated_at": now, "expires_at": now + 3600,
                     "expected_remaining_wait_seconds": 2 * TRANSFER_BREAK_EVEN_SECONDS}
        park = service.park

        def refuse_capture(sandbox_id, *, operation_id, background, pause):
            if not pause:
                parks.append(("refused", False))
                raise WarmParkDeferred(5.0)  # No disk for the capture.
            return park(sandbox_id, operation_id=operation_id, background=background, pause=pause)

        service.park = refuse_capture
        record, _ = runtime.park_with_activity_revision(
            "agent", operation_id="relay-park:1", generation=1, relay_request_id="r1",
            resource_phase=long_hint)
        self.assertEqual(record.state, "running")
        self.assertEqual(parks, [("refused", False), ("relay-park", True)])
        self.assertEqual(service.warden.paused, {("agent", 1)})
        with self.assertRaises(WarmParkDeferred):  # An explicit park still needs a capture.
            runtime.park_with_activity_revision("agent", operation_id="park:2")


if __name__ == "__main__":
    unittest.main()
