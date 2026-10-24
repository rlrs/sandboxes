"""Bulk work queues fairly without starving wakes or control requests."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Lock
import time
import unittest

from tests import test_direct_provisioner as direct_fixtures
from tests.admission_backends import AdmissionBackendCase
from sandboxes.direct_service import DirectSandboxService

TEST_TIER = "contract"


def wait_queued(limiter, count):
    deadline = time.monotonic() + 3
    while len(limiter._waiters) != count:
        if time.monotonic() > deadline:
            raise AssertionError("admission did not queue")
        time.sleep(0.005)


class StartupAdmissionTests(AdmissionBackendCase, unittest.TestCase):
    def test_256_requests_queue_and_release_permits_on_failure(self):
        from sandboxes.models import ResourceQuantity

        # A cleanup, not a with-block: cleanups run last-in first-out, so noded
        # (installed by admitted()) stops before its registry directory goes.
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        fixture = direct_fixtures.DirectProvisionerTests()
        provisioner, *_ = fixture.make(Path(directory.name).resolve())
        service = self.admitted(DirectSandboxService(provisioner, max_concurrent_startups=8))
        release, full = Event(), Event()
        lock = Lock()
        active = peak = 0

        def work(index):
            nonlocal active, peak
            try:
                with service.admission.startup((f"request-{index}", 1), ResourceQuantity(), managed=False):
                    with lock:
                        active += 1
                        peak = max(peak, active)
                        if active == 8:
                            full.set()
                    try:
                        # The agent's reentrant helpers reuse the outer slot.
                        with nullcontext() if self.noded else service.startup_admission():
                            if not release.wait(3):
                                raise TimeoutError("test gate")
                        if index == 0:
                            raise ValueError("injected failure")
                    finally:
                        with lock:
                            active -= 1
                return "done"
            except ValueError:
                return "failed"

        with ThreadPoolExecutor(max_workers=32) as pool:
            futures = [pool.submit(work, i) for i in range(256)]
            try:
                self.assertTrue(full.wait(3))
            finally:
                release.set()
            outcomes = [f.result(5) for f in futures]
        self.assertEqual(peak, 8)
        self.assertEqual(outcomes.count("done"), 255)
        self.assertEqual(outcomes.count("failed"), 1)
        self.assertEqual(active, 0)
        self.assertEqual(self.slots_available(service, "startup"), 8)


class ReclaimDemandTests(unittest.TestCase):
    def test_startup_demand_deduplicates_active_lease_and_cleans_failures(self):
        from sandboxes.models import ResourceQuantity
        from sandboxes.transition_admission import MemoryDemand

        with TemporaryDirectory() as directory:
            provisioner, *_ = direct_fixtures.DirectProvisionerTests().make(Path(directory).resolve())
            service = DirectSandboxService(provisioner)
            requested = ResourceQuantity(vcpu=1, memory_mb=256)
            with self.assertRaisesRegex(ValueError, 'injected'):
                with service._startup_demand('first', 1, requested):
                    with service._startup_demand('first', 1, requested):
                        self.assertEqual(service.reclaim_demand()[1].physical_bytes, 256 * 1024**2)
                    with service._startup_demand('second', 1, requested):
                        self.assertEqual(service.reclaim_demand()[1].physical_bytes, 256 * 1024**2)
                    self.assertEqual(service.reclaim_demand()[1].physical_bytes, 256 * 1024**2)
                    raise ValueError('injected')
            self.assertEqual(service.reclaim_demand()[1].physical_bytes, 0)
            self.assertEqual(service.transition_admission_snapshot()["startup"]["waiting"], 0)
            service.close_admission()
            # A drain bills paused reclaim nothing.
            self.assertEqual(service.reclaim_demand(), (False, MemoryDemand()))

