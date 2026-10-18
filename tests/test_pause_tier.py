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
from tests import test_resident_memory as resident_fixtures
from sandboxes import pause_tier
from sandboxes.background_io import Pressure
from sandboxes.direct_service import DirectSandboxService
from sandboxes.direct_warden import CommandResult, DirectRunscWarden, DirectWardenError
from sandboxes.hibernation import HibernationState
from sandboxes.node_agent import NodeAgentHandler
from sandboxes.node_runtime import DirectNodeRuntime
from sandboxes.pause_tier import (
    PREFETCH_MIN_SWAP_BYTES,
    TRANSFER_BREAK_EVEN_SECONDS,
    PausedWait,
    PauseStats,
    ReclaimBudget,
    cgroup_swap_bytes,
    memory_pieces,
    park_tier,
    prefetch,
    reclaim_stalled,
    relief_plan,
    MAX_STALLS,
    STALL_BACKOFF_SECONDS,
    STALL_BYTES,
    note_reclaim,
    ZSWAP_SHARE_OF_BOUND,
    cap_zswap,
)
from sandboxes.resident_memory import ResidentMemoryReclaimer
from sandboxes.sandbox import SandboxConflictError
from sandboxes.transition_admission import MemoryDemand
from sandboxes.warm_park import WarmParkDeferred, decide_resident_wait

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

    def test_reclaim_runs_only_under_pressure_and_evicts_the_longest_expected_idle(self):
        relaxed = decide_resident_wait(
            Pressure(0.9, 0.0, 0.0, 90 * GIB), SimpleNamespace(physical_bytes=0, ram_backing_bytes=0))
        pressed = decide_resident_wait(
            Pressure(0.01, 0.0, 0.0, GIB), SimpleNamespace(physical_bytes=0, ram_backing_bytes=0))
        self.assertTrue(pressed.reclaim and pressed.target_bytes > 2 * GIB)
        waits = {
            ("short", 1): PausedWait(paused_at=90.0, resident_bytes=4 * GIB),   # idle 10 s
            ("long", 1): PausedWait(paused_at=0.0, resident_bytes=GIB),         # idle 100 s
            ("hinted", 1): PausedWait(paused_at=99.0, expected_until=400.0, resident_bytes=GIB),
            ("unmeasured", 1): PausedWait(paused_at=0.0),
        }
        self.assertEqual(relief_plan(relaxed, waits, now=100.0, swap_room=INF), ((), ()))
        plan, escalations = relief_plan(pressed, waits, now=100.0, swap_room=INF)
        # Hinted waits by expected idle x resident; without a hint the most
        # recently paused go first: the longest paused is the likeliest to wake.
        self.assertEqual([key for key, _ in plan][:3], [("hinted", 1), ("short", 1), ("long", 1)])
        self.assertNotIn(("unmeasured", 1), dict(plan))
        self.assertLessEqual(sum(target for _, target in plan), pressed.target_bytes)
        self.assertEqual(escalations, ())  # Swap has room and nothing stalled.
        # In-flight reclaim counts its target against the same deficit.
        waits[("hinted", 1)].reclaiming = pressed.target_bytes
        self.assertEqual(relief_plan(pressed, waits, now=100.0, swap_room=INF), ((), ()))

    def test_what_swap_cannot_hold_or_a_stalled_reclaim_escalates_to_hibernate(self):
        pressed = decide_resident_wait(  # A 6.5 GiB deficit.
            Pressure(0.01, 0.0, 0.0, GIB), SimpleNamespace(physical_bytes=0, ram_backing_bytes=0))

        def waits():
            return {
                ("hinted", 1): PausedWait(paused_at=99.0, expected_until=400.0, resident_bytes=GIB),
                ("long", 1): PausedWait(paused_at=0.0, resident_bytes=GIB),
                ("short", 1): PausedWait(paused_at=90.0, resident_bytes=4 * GIB),
                ("small", 1): PausedWait(paused_at=0.0, resident_bytes=STALL_BYTES - 1),
                ("swapped", 1): PausedWait(paused_at=0.0, resident_bytes=GIB // 8,
                                           swapped_bytes=GIB),
                ("unmeasured", 1): PausedWait(paused_at=0.0),
            }

        # 1.5 GiB of swap room: the best two swap out, the rest hibernates.
        reclaims, escalations = relief_plan(pressed, waits(), now=100.0, swap_room=3 * GIB // 2)
        self.assertEqual(reclaims, ((("hinted", 1), GIB), (("short", 1), GIB // 2)))
        self.assertEqual(escalations, (("long", 1),))
        # Swap nearly full: every wait worth a hibernate escalates, best first.
        # A capture faults swapped pages in first: a mostly swapped wait, or a
        # tiny one, would take more RAM than it frees.
        self.assertEqual(relief_plan(pressed, waits(), now=100.0, swap_room=0),
                         ((), (("hinted", 1), ("short", 1), ("long", 1))))
        # One stall only backs off: no reclaim and no hibernate until it ends.
        backing_off = waits()
        note_reclaim(backing_off[("hinted", 1)], True, now=100.0)
        reclaims, escalations = relief_plan(pressed, backing_off, now=101.0, swap_room=INF)
        self.assertNotIn(("hinted", 1), {*dict(reclaims), *escalations})
        reclaims, _ = relief_plan(pressed, backing_off, now=100.0 + STALL_BACKOFF_SECONDS, swap_room=INF)
        self.assertIn(("hinted", 1), dict(reclaims))  # Retried after the backoff.
        # MAX_STALLS in a row escalate even with room; others still swap.
        stalled = waits()
        stalled[("hinted", 1)].stalls = MAX_STALLS
        reclaims, escalations = relief_plan(pressed, stalled, now=100.0, swap_room=INF)
        self.assertEqual((dict(reclaims).keys(), escalations),
                         ({("long", 1), ("short", 1), ("small", 1), ("swapped", 1)}, (("hinted", 1),)))
        stalled[("swapped", 1)].stalls = MAX_STALLS  # Already reclaimed: left alone.
        self.assertNotIn(("swapped", 1), relief_plan(pressed, stalled, now=100.0, swap_room=0)[1])
        # An in-flight escalation counts against the deficit.
        busy = waits()
        busy[("short", 1)].escalating = True
        busy[("short", 1)].resident_bytes = pressed.target_bytes
        self.assertEqual(relief_plan(pressed, busy, now=100.0, swap_room=0), ((), ()))
        # An in-flight reclaim fills swap with its target, not with all it holds:
        # counting 4 GiB would leave 1 GiB of room and escalate needlessly.
        inflight = {("a", 1): PausedWait(0.0, resident_bytes=4 * GIB, reclaiming=GIB),
                    ("b", 1): PausedWait(0.0, resident_bytes=4 * GIB)}
        self.assertEqual(relief_plan(pressed, inflight, now=100.0, swap_room=5 * GIB),
                         (((("b", 1), 4 * GIB),), ()))

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
        self.config = replace(self.config, pause_tier=True)
        self.runner.identity_config = self.config
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

    def test_pause_requires_the_flag_and_a_running_journal(self):
        disabled = DirectRunscWarden(replace(self.config, pause_tier=False), runner=self.runner,
                                     fencer=self.fencer, storage=self.storage,
                                     rootfs_lifecycle=self.rootfs)
        with self.assertRaisesRegex(DirectWardenError, "disabled"):
            disabled.pause(self.sandbox)
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


class PausedReclaimTests(unittest.TestCase):
    setUp = resident_fixtures.ResidentMemoryTests.setUp  # The fake cgroup tree.
    sample = resident_fixtures.ResidentMemoryTests.sample

    def _anonymous(self, current):
        (self.path / "memory.current").write_text(str(current))
        (self.path / "memory.stat").write_text(
            f"shmem {current}\nanon 0\nfile {current}\nfile_dirty 0\nfile_writeback 0\n"
            "workingset_refault_file 0\n")

    def test_swappiness_reclaim_moves_tmpfs_until_the_cgroup_stops_shrinking(self):
        (self.path / "memory.reclaim").touch()
        self._anonymous(1000)
        sample = self.sample()
        writes = []

        def write(_fd, data):
            writes.append(data)
            self._anonymous(1000 - 200 * min(len(writes), 2))  # Shrinks twice, then stalls.
            return len(data)

        with patch("os.write", side_effect=write):
            report = ResidentMemoryReclaimer(self.sampler).reclaim(
                ("s", 1), sample, target_bytes=1000, is_current=lambda: True,
                window_bytes=200, swappiness=200)
        self.assertEqual(writes[0], b"200 swappiness=200")
        (self.path / "memory.swap.current").write_text("400")
        self.assertEqual(self.sample().swap_bytes, 400)  # Footprint, not resident.
        self.assertEqual(report.reason, "not_shrinking")
        self.assertEqual(report.reclaimed_bytes, 400)
        self.assertEqual(len(writes), 3)

    def test_thaw_mid_reclaim_cancels_the_next_window(self):
        (self.path / "memory.reclaim").touch()
        self._anonymous(1000)
        thawed = threading.Event()

        def write(_fd, data):
            self._anonymous(800)
            thawed.set()  # A thaw lands while this window is in the kernel.
            return len(data)

        with patch("os.write", side_effect=write) as written:
            report = ResidentMemoryReclaimer(self.sampler).reclaim(
                ("s", 1), self.sample(), target_bytes=1000, window_bytes=200, swappiness=200,
                is_current=lambda: not thawed.is_set())
        self.assertEqual(written.call_count, 1)
        self.assertEqual((report.reason, report.reclaimed_bytes), ("superseded", 200))

    def _shrinking_writes(self, priorities=None):
        level, guard = [1000], threading.Lock()

        def write(_fd, data):
            if priorities is not None:
                priorities.append(os.getpriority(os.PRIO_PROCESS, 0))
            with guard:
                level[0] -= 100
                self._anonymous(level[0])
            return len(data)
        return write

    def test_a_thaw_while_waiting_for_the_budget_cancels_before_any_write(self):
        (self.path / "memory.reclaim").touch()
        self._anonymous(1000)
        budget = ReclaimBudget(bytes_per_second=100)  # Next window in a second.
        thawed = threading.Event()
        threading.Timer(0.1, thawed.set).start()
        with patch("os.write", side_effect=self._shrinking_writes()) as written:
            started = time.monotonic()
            report = ResidentMemoryReclaimer(self.sampler).reclaim(
                ("s", 1), self.sample(), target_bytes=300, window_bytes=100, swappiness=200,
                is_current=lambda: not thawed.is_set(), budget=budget)
        self.assertEqual((written.call_count, report.reason), (1, "superseded"))
        self.assertLess(time.monotonic() - started, 0.5)

    def test_only_the_kernel_write_runs_at_background_priority(self):
        (self.path / "memory.reclaim").touch()
        self._anonymous(1000)
        budget = ReclaimBudget(bytes_per_second=1e12)
        calls, nice = [], [0]

        def setpriority(_which, _who, value):
            calls.append(value)
            nice[0] = value

        priorities = []
        with patch.object(pause_tier.os, "getpriority", side_effect=lambda *_: nice[0]), \
                patch.object(pause_tier.os, "setpriority", side_effect=setpriority), \
                patch("os.write", side_effect=self._shrinking_writes(priorities)):
            budget._restorable = True  # As root (CAP_SYS_NICE) on a node.
            ResidentMemoryReclaimer(self.sampler).reclaim(
                ("s", 1), self.sample(), target_bytes=200, window_bytes=100, swappiness=200,
                is_current=lambda: True, budget=budget)
            self.assertEqual((priorities, calls, nice[0]), ([19, 19], [19, 0, 19, 0], 0))
            with patch("os.write", side_effect=OSError(errno.EIO, "swap device")):
                report = ResidentMemoryReclaimer(self.sampler).reclaim(
                    ("s", 1), self.sample(), target_bytes=200, window_bytes=100, swappiness=200,
                    is_current=lambda: True, budget=budget)
            self.assertEqual((report.reason, nice[0]), ("kernel_error", 0))  # A failed write too.
            # Without CAP_SYS_NICE the write could never return to normal.
            budget._restorable = False
            calls.clear()
            with budget.background():
                pass
        self.assertEqual(calls, [])

    def test_budget_reservations_queue_and_a_cancelled_wait_returns_promptly(self):
        clock = [10.0]
        budget = ReclaimBudget(bytes_per_second=100, clock=lambda: clock[0],
                               sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds))
        self.assertTrue(budget.admit(100, lambda: True))  # Idle budget: immediate.
        self.assertEqual(clock[0], 10.0)
        self.assertTrue(budget.admit(50, lambda: True))  # Starts where the last ends.
        self.assertAlmostEqual(clock[0], 11.0)
        checks = []
        self.assertFalse(budget.admit(100, lambda: checks.append(1) and False))
        self.assertEqual(len(checks), 1)
        self.assertAlmostEqual(clock[0], 11.0)
        with self.assertRaises(ValueError):
            ReclaimBudget(concurrency=0)


class _PausingWarden(provisioner_fixtures.FakeWarden):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.config.pause_tier = True
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

    def _pause(self):
        record = self.service.park(self.sandbox_id, operation_id="idle-park:1", pause=True)
        self.assertEqual(record.state, "running")  # The gateway still sees it running.
        self.assertIn(self.key, self.warden.paused)
        self.warden.events.clear()

    def test_a_thaw_supersedes_paused_reclaim_from_its_prefetch_on(self):
        self._pause()
        windows = []

        def reclaim():
            return self.service.reclaim_paused(*self.key, target_bytes=GIB, is_current=lambda: True,
                                               budget=None)

        with patch.object(self.service, "_sample_resident", return_value=object()), \
                patch.object(ResidentMemoryReclaimer, "reclaim",
                             lambda _self, *_a, is_current, **_k: windows.append(is_current)):
            reclaim()
            self.assertTrue(windows[0]())
            # A thaw prefetches before `runsc resume` removes the marker: its
            # reads would race a reclaim still swapping the same memory out.
            self.warden.prefetching.add(self.key)
            self.assertFalse(windows[0]())
            reports = [reclaim()]
            self.warden.prefetching.clear()
            self.warden.thaw(self.service._require_registration(self.sandbox_id).to_direct_sandbox())
            reports.append(reclaim())  # The thaw won the race with the plan.
        self.assertEqual(len(windows), 1)
        for report in reports:  # Superseded, not stalled: no needless hibernate.
            self.assertEqual((report.reason, reclaim_stalled(report, GIB)), ("superseded", False))

    def test_heartbeat_footprint_counts_swapped_out_pages(self):
        sample = SimpleNamespace(current_bytes=GIB, swap_bytes=3 * GIB, sampled_at=time.monotonic())
        handler = SimpleNamespace(manager=SimpleNamespace(service=SimpleNamespace(
            cached_resident_memory_sample=lambda *_: sample)))
        entry = NodeAgentHandler._sandbox_inventory_entry(handler, self.created)
        self.assertEqual(entry.memory_observation.memory_bytes, 4 * GIB)


class PauseNodeRuntimeTests(unittest.TestCase):
    def _runtime(self, *, pressure=None):
        warden = SimpleNamespace(
            config=SimpleNamespace(pause_tier=True, application_memory_root=None),
            paused=set(), pause_stats=PauseStats())
        warden.is_paused = lambda sandbox_id, generation: (sandbox_id, generation) in warden.paused
        warden.paused_keys = lambda: sorted(warden.paused)
        registration = SimpleNamespace(
            phase="owned", sandbox_id="agent", sandbox_generation=1,
            spec=SimpleNamespace(parkable=True, managed_process=False))
        registry = SimpleNamespace(
            load_drain=lambda: SimpleNamespace(draining=False),
            relay_wake_fence=Mock(return_value=False),
            activity_revision=lambda: 1,
            snapshot=lambda: SimpleNamespace(records=(registration,), activity_revision=1))
        parks = []

        def park(sandbox_id, *, operation_id, background, pause):
            parks.append((operation_id.split(":")[0], pause))
            if pause:
                warden.paused.add((sandbox_id, 1))
            else:  # A hibernate thaws first.
                warden.paused.discard((sandbox_id, 1))
            return SimpleNamespace(state="running" if pause else "parked")

        service = SimpleNamespace(
            warden=warden, provisioner=SimpleNamespace(registry=registry),
            open_admission=lambda: None, close_admission=lambda: None,
            idle_park_seconds=0.01, park=park,
            idle_for_seconds=lambda *_a, **_k: 1.0,
            get=lambda sandbox_id: SimpleNamespace(state="running"),
            get_snapshot=lambda sandbox_id: SimpleNamespace(state="running"),
            resident_memory_sample=lambda *_: SimpleNamespace(current_bytes=GIB, swap_bytes=0),
            resident_demand_snapshot=lambda: {
                "admitted_demand_bytes": 0, "pending_demand_bytes": 0,
                "unknown_transition_memory_costs": 0},
            advance_lifecycle_activity_revision=lambda: 7,
            reclaim_paused=Mock(return_value=SimpleNamespace(
                reclaimed_bytes=GIB // 2, elapsed_seconds=0.25, reason="superseded")),
        )
        runtime = DirectNodeRuntime(service)
        if pressure is not None:
            runtime._warm_parks.pressure = lambda: pressure
        return runtime, service, parks

    def test_idle_timer_pauses_and_adopts_existing_pauses(self):
        runtime, service, parks = self._runtime()
        runtime.start()
        try:
            deadline = time.monotonic() + 2
            while not parks and time.monotonic() < deadline:
                time.sleep(0.01)
            time.sleep(0.05)  # Further ticks find the marker and do not re-park.
        finally:
            runtime.stop()
        self.assertEqual(parks, [("idle-park", True)])
        self.assertIn(("agent", 1), runtime._paused)

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
        self.assertIsNotNone(runtime._paused[("agent", 1)].expected_until)
        with self.assertRaises(WarmParkDeferred):  # An explicit park still needs a capture.
            runtime.park_with_activity_revision("agent", operation_id="park:2")

    def test_drain_demand_alone_never_swaps_out_paused_sandboxes(self):
        runtime, service, _ = self._runtime(pressure=Pressure(0.9, 0.0, 0.0, 90 * GIB))
        service.warden.paused.add(("agent", 1))
        runtime._warm_parks.demand = lambda: MemoryDemand(1 << 63, 1 << 63)  # Closed admission.
        service.admission_open = False
        runtime._reclaim_paused_tick()
        service.reclaim_paused.assert_not_called()
        service.admission_open = True  # The same demand from open admission is real,
        service.warden.paused.update({("agent2", 1), ("agent3", 1)})
        runtime._reclaim_paused_tick()  # but only forecasts: a burst gets 1 s to settle,
        service.reclaim_paused.assert_not_called()
        runtime._paused_forecast_since -= 1.0
        runtime._reclaim_paused_tick()  # then waits are released one at a time.
        runtime._relay_park_executor.shutdown(wait=True)
        service.reclaim_paused.assert_called_once()


    @staticmethod
    def _drain(runtime):
        """Wait for submitted reclaims and escalations; the next tick makes a new pool."""
        executor, runtime._relay_park_executor = runtime._relay_park_executor, None
        if executor is not None:
            executor.shutdown(wait=True)

    def test_stalled_or_failed_reclaims_back_off_then_escalate_after_max_stalls(self):
        for outcome in (SimpleNamespace(reclaimed_bytes=MIB, elapsed_seconds=1.0, reason="not_shrinking"),
                        DirectWardenError("memory.reclaim is unsupported"), TypeError("a bug")):
            with self.subTest(outcome=outcome):
                runtime, service, parks = self._runtime(pressure=Pressure(0.01, 0.0, 0.0, GIB))
                service.warden.paused.add(("agent", 1))
                service.reclaim_paused.side_effect = [outcome] * MAX_STALLS
                for stall in range(1, MAX_STALLS + 1):
                    runtime._reclaim_paused_tick()
                    self._drain(runtime)
                    wait = runtime._paused[("agent", 1)]
                    self.assertEqual((wait.stalls, parks), (stall, []))
                    runtime._reclaim_paused_tick()  # Backing off: nothing happens.
                    self._drain(runtime)
                    self.assertEqual(service.reclaim_paused.call_count, stall)
                    wait.retry_at = 0.0  # The backoff ends.
                runtime._reclaim_paused_tick()
                self._drain(runtime)
                self.assertEqual(parks, [("pause-escalation", False)])
                snapshot = runtime.resident_wait_snapshot()
                self.assertEqual((snapshot["pause_reclaim_stalls"], snapshot["pause_escalations"]),
                                 (MAX_STALLS, 1))
                stopped = "pause_reclaim_not_shrinking" if isinstance(outcome, SimpleNamespace) \
                    else "pause_reclaim_errors"
                self.assertEqual(snapshot[stopped], MAX_STALLS)

    def test_escalation_loses_to_activity_and_a_refused_capture_stays_paused(self):
        runtime, service, parks = self._runtime()
        runtime._paused[("agent", 1)] = PausedWait(0.0, resident_bytes=GIB, escalating=True)
        runtime._escalate_paused(("agent", 1))  # Thawed after the plan: no marker.
        self.assertEqual(parks, [])
        self.assertFalse(runtime._paused[("agent", 1)].escalating)
        with self.assertRaisesRegex(SandboxConflictError, "lost to activity"):
            runtime.park_with_activity_revision("agent", operation_id="pause-escalation:1",
                                                generation=1, escalate=True)
        service.warden.paused.add(("agent", 1))
        service.park = Mock(side_effect=WarmParkDeferred(5.0))  # No disk for the capture.
        runtime._paused[("agent", 1)].escalating = True
        runtime._escalate_paused(("agent", 1))
        service.park.assert_called_once()
        self.assertEqual(service.park.call_args.kwargs["background"], False)  # No upload.
        self.assertFalse(runtime._paused[("agent", 1)].escalating)  # The next tick retries.
        self.assertIn(("agent", 1), service.warden.paused)
        self.assertEqual(runtime.resident_wait_snapshot()["pause_escalations"], 0)

    def test_a_restart_after_a_crashed_escalation_escalates_again(self):
        full = Pressure(0.01, 0.0, 0.0, GIB, swap_total_bytes=16 * GIB, swap_free_bytes=0)
        runtime, service, parks = self._runtime(pressure=full)
        service.warden.paused.add(("agent", 1))
        park = service.park
        for error in (TypeError("a bug"), SystemExit("node agent killed")):
            service.park = Mock(side_effect=error)
            runtime._reclaim_paused_tick()
            self._drain(runtime)
            service.park.assert_called_once()  # Any error returns the slot.
            self.assertFalse(runtime._paused[("agent", 1)].escalating)
            self.assertIn(("agent", 1), service.warden.paused)  # The marker survives.
        service.park = park
        restarted = DirectNodeRuntime(service)
        restarted._warm_parks.pressure = lambda: full
        restarted._reclaim_paused_tick()
        self._drain(restarted)
        self.assertEqual((parks, service.warden.paused), ([("pause-escalation", False)], set()))


if __name__ == "__main__":
    unittest.main()
