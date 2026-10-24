"""Phase 3a of the Rust node daemon, the agent's half (docs/architecture.md).

runtime/noded owns the pause tier: the agent runs none of its loops, fences its own ops with A shared, marks its thaws on the marker's
flock, executes escalations and growth events for noded, publishes the demand
paused reclaim bills and adds noded's counters to its heartbeat
(sandboxes/pause_handoff.py). Subprocesses play noded's locks.
"""

from dataclasses import fields, replace
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
import time
import unittest
from unittest.mock import patch

from tests import test_direct_provisioner as fixtures
from tests.test_pause_tier import PauseWardenTests, _PausingWarden
from tests.test_rust_exec_fence import TOKEN, _UnixConnection
from sandboxes import pause_handoff, pause_tier
from sandboxes.sandbox import SandboxExecAdmissionDeferredError, WarmParkDeferred
from sandboxes.direct_service import DirectSandboxService
from sandboxes.models import ResidentWaitMetrics
from sandboxes.node_runtime import DirectNodeRuntime
from sandboxes.transition_admission import MemoryDemand

TEST_TIER = "contract"
# noded's lock attempts, one per argument "<path>:<operation>", in order:
# "ex" LOCK_EX|LOCK_NB (a pause's T and A), "sh" LOCK_SH|LOCK_NB (a reclaim
# probe), "hold" LOCK_EX held until stdin closes (a thaw or a pause in flight).
_NODED = """
import fcntl, os, sys
held = []
for item in sys.argv[1:]:
    path, operation = item.rsplit(":", 1)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC if operation != "ex" else os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
    fd = os.open(path, flags, 0o600)
    try:
        fcntl.flock(fd, (fcntl.LOCK_SH if operation == "sh" else fcntl.LOCK_EX) | fcntl.LOCK_NB)
    except BlockingIOError:
        print("busy", flush=True)
        sys.exit(0)
    held.append(fd)
print("held", flush=True)
if any(item.endswith(":hold") for item in sys.argv[1:]):
    sys.stdin.read()
"""


def noded(*locks) -> str:
    return subprocess.run([sys.executable, "-c", _NODED, *map(str, locks)], input="", capture_output=True,
                          text=True, check=True, timeout=10).stdout.strip()


class _Holder:
    def __init__(self, path: Path) -> None:
        self.process = subprocess.Popen([sys.executable, "-c", _NODED, f"{path}:hold"], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, text=True)
        assert self.process.stdout.readline().strip() == "held"

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.stdin.close()
            self.process.wait(5)
        self.process.stdout.close()


def runtime_fixture(test):
    directory = TemporaryDirectory()
    test.addCleanup(directory.cleanup)
    root = Path(directory.name).resolve()
    with patch.object(fixtures, "FakeWarden", _PausingWarden):
        provisioner, _, _, _, warden = fixtures.DirectProvisionerTests().make(root)
    warden.config.runtime_root = root / "runsc"
    (root / "runsc" / "warden-locks").mkdir(parents=True)
    service = DirectSandboxService(provisioner, process_runner=fixtures.FakeProcessRunner())
    runtime = DirectNodeRuntime(service)
    test.addCleanup(runtime.stop)
    spec = replace(fixtures.DirectProvisionerTests.spec(), parkable=True)
    record = fixtures.DirectProvisionerTests.create(service, spec)
    return root, service, runtime, (spec.id, record.generation)


class ActivityFenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root, self.service, self.runtime, self.key = runtime_fixture(self)
        fence = self.runtime.exec_fence
        self.t, self.a = fence.transition_path(self.key[0]), fence.activity_path(self.key[0])

    def rust_pause(self) -> str:
        return noded(f"{self.t}:ex", f"{self.a}:ex")

    def test_a_python_op_holds_a_shared_so_a_rust_pause_cannot_land(self) -> None:
        lifecycle = self.runtime.lifecycle
        self.assertEqual(self.rust_pause(), "held")
        lifecycle.acquire_shared(self.key[0])
        lifecycle.acquire_shared(self.key[0])  # two concurrent Python ops
        self.assertEqual(self.rust_pause(), "busy")
        self.assertEqual(noded(f"{self.t}:ex"), "held")  # T went once A was held
        lifecycle.release_shared(self.key[0])
        self.assertEqual(self.rust_pause(), "busy")
        lifecycle.release_shared(self.key[0])
        self.assertEqual(self.rust_pause(), "held")
        self.assertEqual(lifecycle._activity_holds, {})
        with lifecycle.shared(self.key[0]), self.assertRaises(RuntimeError):
            self.assertEqual(self.rust_pause(), "busy")
            raise RuntimeError("an op failed")
        self.assertEqual(self.rust_pause(), "held")

    def test_a_rust_pause_in_flight_delays_a_python_op(self) -> None:
        self.t.touch()
        pause = _Holder(self.t)  # noded between its T and its pause's end
        self.addCleanup(pause.close)
        entered = Event()

        def op():
            with self.runtime.lifecycle.shared(self.key[0]):
                entered.set()

        thread = Thread(target=op)
        thread.start()
        self.assertFalse(entered.wait(0.2))
        pause.close()
        self.assertTrue(entered.wait(5))
        thread.join(5)

    def test_a_transition_held_past_the_admission_wait_defers_the_op(self) -> None:
        self.t.touch()
        pause = _Holder(self.t)  # a pause stuck holding T
        self.addCleanup(pause.close)
        self.service.admission_wait_seconds = 0.05
        with self.assertRaisesRegex(SandboxExecAdmissionDeferredError, "timed out waiting"):
            self.runtime.lifecycle.acquire_shared(self.key[0])
        pause.close()
        self.assertEqual(self.rust_pause(), "held")  # the deferred op holds nothing

    def test_a_failed_op_start_releases_a(self) -> None:
        with patch.object(self.service, "running_timings", side_effect=RuntimeError("gone")):
            with self.assertRaises(RuntimeError):
                self.runtime.lifecycle.acquire_shared(self.key[0])
        self.assertEqual(self.rust_pause(), "held")

class MarkerFlockTests(unittest.TestCase):
    setUp = PauseWardenTests.setUp
    tearDown = PauseWardenTests.tearDown
    _warden = PauseWardenTests._warden
    key = PauseWardenTests.key

    def paused_marker(self) -> Path:
        self.warden.create(self.sandbox, operation_id="create:1")
        self.assertTrue(self.warden.pause(self.sandbox))
        return self.warden._pause_marker(*self.key)

    def test_a_thaw_in_another_process_is_thawing_here(self) -> None:
        marker = self.paused_marker()
        self.assertFalse(self.warden.thawing(*self.key))
        thaw = _Holder(marker)
        self.addCleanup(thaw.close)
        self.assertTrue(self.warden.thawing(*self.key) and pause_tier.marker_thawing(marker))
        thaw.close()
        self.assertFalse(self.warden.thawing(*self.key))
        self.assertFalse(pause_tier.marker_thawing(marker.with_name("none.sandbox-1")))

    def test_python_holds_the_marker_through_resume_and_unlink(self) -> None:
        marker = self.paused_marker()
        run, seen = self.runner.run, []

        def resume(argv, **kwargs):
            if "resume" in argv:  # noded's reclaim probe while the thaw resumes
                seen.append(noded(f"{marker}:sh"))
            return run(argv, **kwargs)

        with patch.object(self.runner, "run", side_effect=resume):
            self.assertIsNotNone(self.warden.thaw(self.sandbox))
        self.assertEqual(seen, ["busy"])
        self.assertFalse(marker.exists())
        self.assertTrue(self.warden.pause(self.sandbox))  # a new inode, never held
        self.assertEqual((noded(f"{marker}:sh"), self.warden.thawing(*self.key)), ("held", False))

    def test_a_reclaim_probe_delays_a_thaw_and_a_vanished_marker_is_no_thaw(self) -> None:
        marker = self.paused_marker()
        probe = _Holder(marker)  # LOCK_EX stands in for a probe's shared lock
        self.addCleanup(probe.close)
        done = Event()
        thread = Thread(target=lambda: (self.warden.thaw(self.sandbox), done.set()))
        thread.start()
        self.assertFalse(done.wait(0.2))
        probe.close()
        self.assertTrue(done.wait(5))
        thread.join(5)
        self.assertFalse(marker.exists())
        self.assertTrue(self.warden.pause(self.sandbox))
        flock = pause_tier.fcntl.flock
        with patch.object(pause_tier.fcntl, "flock", side_effect=lambda fd, op: (marker.unlink(), flock(fd, op))):
            self.assertIsNone(self.warden._thaw_locked(self.sandbox))  # thawed meanwhile

    def test_a_lock_on_a_replaced_marker_is_taken_again(self) -> None:
        marker = self.paused_marker()
        flock, inodes = pause_tier.fcntl.flock, []

        def racing_pause(fd, operation):
            inodes.append(os.fstat(fd).st_ino)
            if len(inodes) == 1:  # a thaw and a new pause replaced it meanwhile
                marker.unlink()
                marker.write_bytes(b"container")
            return flock(fd, operation)

        with patch.object(pause_tier.fcntl, "flock", side_effect=racing_pause):
            with pause_tier.thaw_hold(marker) as held:
                self.assertTrue(held)
                self.assertEqual(noded(f"{marker}:sh"), "busy")
        self.assertEqual(len(inodes), 2)
        self.assertNotEqual(*inodes)


class PauseEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        with patch.object(fixtures, "FakeWarden", _PausingWarden):
            provisioner, _, storage, images, self.warden = fixtures.DirectProvisionerTests().make(self.root)
        config = self.warden.config  # what create_config reads from the real stores
        config.runsc, config.runtime_root = Path("/opt/runsc"), self.root / "runsc"
        config.journal_root = self.root / "journals"
        storage.socket_path, images.root = Path("/run/storage.sock"), self.root / "cache"
        (self.root / "runsc" / "warden-locks").mkdir(parents=True)
        self.service = DirectSandboxService(provisioner, process_runner=fixtures.FakeProcessRunner())
        self.spec = replace(fixtures.DirectProvisionerTests.spec(), parkable=True)
        self.record = fixtures.DirectProvisionerTests.create(self.service, self.spec)
        self.socket = str(self.root / "agent.sock")
        server = fixtures.build_direct_node_agent_server(
            "127.0.0.1", 0, service=self.service, image_file=self.root / "images.json", job_id="job",
            node_id="node", node_epoch="epoch", node_control_bearer_token=TOKEN,
            unix_socket=Path(self.socket))
        self.manager = server.RequestHandlerClass.manager
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

    def call(self, method, path, body=None):
        connection = _UnixConnection(self.socket)
        try:
            payload = None if body is None else json.dumps(body).encode()
            connection.request(method, path, body=payload, headers={
                "Authorization": f"Bearer {TOKEN}", "X-UCloud-Noded-Session": "session-a"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def escalate(self, **overrides):
        return self.call("POST", "/internal/v1/pauses/escalate",
                         {"sandbox_id": self.spec.id, "generation": self.record.generation, **overrides})

    def test_escalation_parks_a_paused_sandbox_or_loses_to_activity(self) -> None:
        self.assertEqual(self.escalate(), (409, {
            "error": "pause escalation lost to activity", "error_code": "escalation_lost", "retryable": False}))
        self.service.park(self.spec.id, operation_id="local-wait-1", pause=True)
        with patch.object(self.service, "park", side_effect=WarmParkDeferred(5.0)):
            status, body = self.escalate()
        self.assertEqual((status, body["error_code"], body["retryable"]), (409, "park_deferred", True))
        self.assertEqual(self.escalate(), (200, {"state": "parked"}))
        self.assertEqual(self.warden.events[-1], "hibernate")
        self.assertEqual(self.manager.resident_wait_snapshot()["pause_escalations"], 1)
        for invalid in ({"generation": 0}, {"sandbox_id": "../x"}, {"extra": 1}):
            status, body = self.escalate(**invalid)
            self.assertEqual((status, body["error_code"]), (400, "invalid_request"))

    def test_growth_events_apply_in_order_and_fail_alone(self) -> None:
        recorded = []
        with patch.object(self.service, "record_local_wait_growth",
                          side_effect=lambda items: recorded.extend(items) or [None, ValueError("gone")]):
            status, body = self.call("POST", "/internal/v1/growth/events", {"items": [
                {"action": "wait", "sandbox_id": "sandbox", "generation": 7, "request_id": "local-wait-a"},
                {"action": "activate", "sandbox_id": "other", "generation": 2, "request_id": "local-wait-b"}]})
        self.assertEqual((status, body), (200, {"errors": [None, "gone"]}))
        self.assertEqual(recorded, [("observe_managed_wait", ("sandbox", 7), "local-wait-a"),
                                    ("resume_managed_continuation", ("other", 2), "local-wait-b")])
        status, body = self.call("POST", "/internal/v1/growth/events", {"items": [{"action": "park"}]})
        self.assertEqual(status, 400)

    def test_the_configuration_and_the_demand_file(self) -> None:
        status, effective = self.call("GET", "/internal/v1/creates/config")
        pause = effective["pause"]
        noded_dir = self.root / "noded"
        self.assertEqual((pause["status_path"], pause["agent_demand_path"], pause["warden_paused_dir"]),
                         (str(noded_dir / "status.json"), str(noded_dir / "agent-demand.json"),
                          str(self.root / "runsc" / "warden-paused")))
        self.assertEqual(pause["local_waits"]["nflog_group"], 4207)
        self.assertEqual(pause["reclaim"]["window_bytes"], pause_tier.RECLAIM_WINDOW_BYTES)
        demand = json.loads((noded_dir / "agent-demand.json").read_text())
        self.assertEqual(set(demand), {"seq", "admission_open", "physical_bytes", "ram_backing_bytes"})
        self.assertEqual(noded_dir.stat().st_mode & 0o777, 0o700)

class DemandAndStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root, self.service, self.runtime, self.key = runtime_fixture(self)
        self.noded_dir = self.root / "noded"
        self.noded_dir.mkdir(mode=0o700)

    def demand(self):
        return json.loads((self.noded_dir / "agent-demand.json").read_text())

    def test_the_demand_file_follows_admission_and_closed_bills_nothing(self) -> None:
        publisher = self.runtime._demand_publisher
        with patch.object(self.service, "_next_memory_demand_locked", return_value=MemoryDemand(3, 5)):
            publisher.publish()
            first = self.demand()
            self.assertEqual({k: first[k] for k in first if k != "seq"},
                             {"admission_open": True, "physical_bytes": 3, "ram_backing_bytes": 5})
            self.service.close_admission()
            publisher.publish()
        self.assertEqual(self.demand(), {"seq": first["seq"] + 1, "admission_open": False,
                                         "physical_bytes": 0, "ram_backing_bytes": 0})
        self.assertEqual(os.stat(self.noded_dir / "agent-demand.json").st_mode & 0o777, 0o600)
        self.service.open_admission()
        self.runtime.start()
        deadline = time.monotonic() + 5
        while self.demand()["admission_open"] is not True and time.monotonic() < deadline:
            time.sleep(0.01)
        seq = self.demand()["seq"]
        self.service.close_admission()  # wakes the publisher
        while self.demand()["admission_open"] and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertGreater(self.demand()["seq"], seq)
        self.assertFalse(self.demand()["admission_open"])
        self.runtime.stop()
        self.assertFalse((self.noded_dir / "agent-demand.json").exists())

    def status(self, *, session="s1", seq=1, age=0.0, **stats):
        counters = dict.fromkeys(pause_tier.PAUSE_STAT_NAMES, 0) | stats
        path = self.noded_dir / "status.json"
        pause_handoff.write_atomic(path, {"seq": seq, "session": session, "pause_stats": counters,
                                          "paused_sandboxes": 1, "transient_revision": 9})
        os.utime(path, (time.time() - age, time.time() - age))

    def snapshot(self):
        snapshot = self.runtime.resident_wait_snapshot()
        self.assertIsNotNone(ResidentWaitMetrics.from_dict(snapshot))
        self.assertEqual(set(snapshot), {item.name for item in fields(ResidentWaitMetrics)})
        return snapshot

    def test_the_heartbeat_adds_nodeds_counters_and_keeps_its_names(self) -> None:
        self.service.warden.pause_stats.add(thaws=2, thaw_ms_max=40, pause_escalations=1)
        self.service.warden.paused.add(self.key)
        own = self.snapshot()  # no status.json yet: the agent's own
        self.assertEqual((own["thaws"], own["pauses"], own["paused_sandboxes"]), (2, 0, 1))
        self.status(pauses=5, thaws=3, thaw_ms_max=90, pause_reclaims=1)
        both = self.snapshot()
        self.assertEqual((both["pauses"], both["thaws"], both["thaw_ms_max"], both["pause_reclaims"],
                          both["pause_escalations"], both["paused_sandboxes"]), (5, 5, 90, 1, 1, 1))
        for invalid in ({"thaws": -1}, {"thaws": True}, {"unknown_counter": 1}):
            with self.subTest(invalid=invalid):
                self.status(seq=2, pauses=7, **invalid)
                self.assertEqual(self.snapshot()["pauses"], 5)  # the last valid read, never less
        self.status(seq=3, age=5.0, pauses=8)  # stale
        self.assertEqual(self.snapshot()["pauses"], 5)
        (self.noded_dir / "status.json").write_text("{")
        self.assertEqual(self.snapshot()["pauses"], 5)
        self.status(seq=0, pauses=9)  # a lower seq in the same session
        self.assertEqual(self.snapshot()["pauses"], 5)
        self.status(session="s2", seq=1, pauses=1, thaw_ms_max=10)  # noded restarted
        restarted = self.snapshot()
        self.assertEqual((restarted["pauses"], restarted["thaw_ms_max"]), (6, 90))


class SamplingTests(unittest.TestCase):
    def test_paused_sandboxes_are_sampled_by_noded_not_the_agent(self) -> None:
        _, service, _runtime, key = runtime_fixture(self)
        service.park(key[0], operation_id="idle-park:1", pause=True)
        with patch.object(service, "_sample_resident") as sample:
            service.refresh_resident_memory()
        self.assertEqual(sample.call_count, 0)


if __name__ == "__main__":
    unittest.main()
