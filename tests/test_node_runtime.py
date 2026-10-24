import unittest
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import Mock

from sandboxes.node_runtime import DirectNodeRuntime
from sandboxes.sandbox import (
    NodeDrainState,
    SandboxBusyError,
    SandboxConflictError,
    SandboxExecAdmissionDeferredError,
)


class _Registry:
    def __init__(self, registrations: tuple[object, ...], path: Path) -> None:
        self._registrations = registrations
        self.path = path

    def load_drain(self) -> NodeDrainState:
        return NodeDrainState()

    def list(self) -> tuple[object, ...]:
        return self._registrations

    def get(self, sandbox_id):
        return next((r for r in self._registrations if r.sandbox_id == sandbox_id), None)

    def activity_revision(self) -> int:
        return 1

    def snapshot(self) -> object:
        return SimpleNamespace(records=self._registrations, activity_revision=1)


class _WakeService:
    idle_park_seconds = 0

    def __init__(self, root: Path) -> None:
        registration = SimpleNamespace(
            phase="owned",
            sandbox_id="agent",
            sandbox_generation=1,
            spec=SimpleNamespace(parkable=True, managed_process=True, memory_mb=256),
        )
        self.provisioner = SimpleNamespace(registry=_Registry((registration,), root / "registry.sqlite"))
        # The exec fence lives in the Warden's lock directory.
        self.warden = SimpleNamespace(config=SimpleNamespace(runtime_root=root / "runsc"))
        self.wake_calls: list[tuple[str, int, str]] = []
        self.park_calls: list[str] = []
        self.publication_pending = False
        self.activity_revision = 100

    def open_admission(self) -> None:
        pass

    def resident_memory_sample(self, _sandbox_id, _generation):
        return None

    def get(self, sandbox_id: str) -> object:
        return SimpleNamespace(spec=SimpleNamespace(id=sandbox_id), state="running")

    def running_timings(self, _sandbox):
        return None  # Parked: the restore path, under the request lock.

    def storage_native_publication_pending(self, _sandbox_id: str) -> bool:
        return self.publication_pending

    def wake(self, sandbox_id: str, *, generation: int, operation_id: str) -> object:
        self.wake_calls.append((sandbox_id, generation, operation_id))
        return SimpleNamespace(state="running")

    def park(self, sandbox_id: str, **_kwargs: object) -> object:
        self.park_calls.append(sandbox_id)
        return SimpleNamespace(state="parked")

    def advance_lifecycle_activity_revision(self) -> int:
        self.activity_revision += 1
        return self.activity_revision


class DirectNodeRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_running_sandbox_commands_and_uploads_skip_the_request_lock(self) -> None:
        # 2026-10-05: every command and file write on a sandbox took its request
        # lock; uploads were refused while one ran (5.6 harness files/s per node).
        service = _WakeService(self.root)
        registration = SimpleNamespace(sandbox_generation=1, to_direct_sandbox=lambda: "agent-1")
        service._require_registration = Mock(return_value=registration)
        service._request_lock = Mock(side_effect=AssertionError("request lock taken"))
        service.mark_activity = Mock()
        service.ensure_running_with_timings = Mock(side_effect=AssertionError("restore path taken"))
        service.running_timings = Mock(return_value={"inspect": 1.0, "total": 1.0})
        service.write_file = Mock()
        manager = DirectNodeRuntime(service)
        manager.lifecycle.acquire_shared("agent")
        manager.lifecycle.release_shared("agent")
        manager.upload_file("agent", "/tmp/x", b"x", expected_generation=1)
        service.write_file.assert_called_once_with("agent", "/tmp/x", b"x", expected_generation=1, fenced=True)
        with self.assertRaises(SandboxConflictError):
            manager.upload_file("agent", "/tmp/x", b"x", expected_generation=2)  # Replaced while it arrived.

    def test_tool_joins_transition_before_reading_registration_or_restoring(self) -> None:
        for deleted in (False, True):
            with self.subTest(deleted=deleted):
                service = _WakeService(self.root)
                registration = SimpleNamespace(
                    sandbox_generation=2, to_direct_sandbox=lambda: "new-generation",
                )
                service._require_registration = Mock(return_value=registration)
                service._request_lock = Mock(side_effect=lambda *_, **__: nullcontext())
                service.mark_activity = Mock()
                service.ensure_running_with_timings = Mock(return_value={})
                manager = DirectNodeRuntime(service)
                entered, finished = Event(), Event()
                failures = []

                def tool():
                    entered.set()
                    try:
                        with manager.lifecycle.shared("agent"):
                            pass
                    except Exception as exc:
                        failures.append(exc)
                    finally:
                        finished.set()

                with manager.lifecycle._coordinator.exclusive("agent"):
                    thread = Thread(target=tool)
                    thread.start()
                    self.assertTrue(entered.wait(1))
                    self.assertFalse(finished.wait(.05))
                    service._require_registration.assert_not_called()
                    service.ensure_running_with_timings.assert_not_called()
                    if deleted:
                        service._require_registration.side_effect = ValueError("sandbox deleted")
                thread.join(1)
                self.assertTrue(finished.is_set())
                if deleted:
                    self.assertEqual([str(exc) for exc in failures], ["sandbox deleted"])
                    service.ensure_running_with_timings.assert_not_called()
                else:
                    self.assertFalse(failures)
                    service._request_lock.assert_called_once_with(
                        "agent", 2, wait_seconds=getattr(service, "admission_wait_seconds", 30.0),
                    )
                    service.ensure_running_with_timings.assert_called_once_with("new-generation")
                with manager.lifecycle._coordinator.exclusive("agent"):
                    pass  # Neither branch leaks an activity lease.

    def test_delete_waits_for_an_in_flight_park_instead_of_conflicting(self) -> None:
        service = _WakeService(self.root)
        service.get = Mock(return_value=None)
        service.delete = Mock()
        manager = DirectNodeRuntime(service)
        done = Event()

        def delete():
            manager.delete("agent", generation=1, operation_id="delete-1")
            done.set()

        with manager.lifecycle._coordinator.exclusive("agent"):  # the park
            thread = Thread(target=delete)
            thread.start()
            self.assertFalse(done.wait(.1))
            service.delete.assert_not_called()
        thread.join(2)
        self.assertTrue(done.is_set())
        service.delete.assert_called_once_with("agent", generation=1)

    def test_tool_transition_timeout_is_retryable_before_exec_acceptance(self) -> None:
        service = _WakeService(self.root)
        service.admission_wait_seconds = .01
        service._require_registration = Mock()
        manager = DirectNodeRuntime(service)
        with manager.lifecycle._coordinator.exclusive("agent"):
            with self.assertRaises(SandboxExecAdmissionDeferredError):
                manager.lifecycle.acquire_shared("agent")
        service._require_registration.assert_not_called()
        with manager.lifecycle._coordinator.exclusive("agent"):
            pass

    def test_activity_release_unwinds_coordinator_when_storage_cleanup_fails(self) -> None:
        for failing_step in ("registration", "mark_activity"):
            with self.subTest(failing_step=failing_step):
                service = _WakeService(self.root)
                service.provisioner.registry.get = Mock(
                    return_value=SimpleNamespace(sandbox_generation=1),
                    side_effect=RuntimeError("registry unavailable") if failing_step == "registration" else None,
                )
                service.mark_activity = Mock(
                    side_effect=RuntimeError("activity update failed") if failing_step == "mark_activity" else None,
                )
                manager = DirectNodeRuntime(service)  # type: ignore[arg-type]
                coordinator = manager.lifecycle._coordinator
                coordinator.acquire_shared("agent")
                with self.assertRaises(RuntimeError):
                    manager.lifecycle.release_shared("agent")
                # The error remains visible while park/delete can proceed.
                with coordinator.exclusive("agent"):
                    pass
                if failing_step == "registration":
                    service.mark_activity.assert_not_called()
                else:
                    service.mark_activity.assert_called_once_with("agent", 1)

    def test_wake_is_idempotent_while_running_activity_is_attached(self) -> None:
        service = _WakeService(self.root)
        manager = DirectNodeRuntime(service)  # type: ignore[arg-type]
        manager.lifecycle._coordinator.acquire_shared("agent")
        try:
            record = manager.wake(
                "agent",
                generation=1,
                operation_id="relay-wake:request-1",
            )
        finally:
            manager.lifecycle._coordinator.release_shared("agent")

        self.assertEqual(record.state, "running")
        self.assertEqual(
            service.wake_calls,
            [("agent", 1, "relay-wake:request-1")],
        )
        self.assertEqual(service.activity_revision, 101)

    def test_wake_joins_an_existing_transition_then_rechecks_state(self) -> None:
        service = _WakeService(self.root)
        manager = DirectNodeRuntime(service)  # type: ignore[arg-type]
        started = Event()
        finished = Event()
        failures: list[BaseException] = []

        def wake() -> None:
            started.set()
            try:
                manager.wake(
                    "agent",
                    generation=1,
                    operation_id="relay-wake:request-2",
                )
            except BaseException as exc:  # pragma: no cover - thread handoff
                failures.append(exc)
            finally:
                finished.set()

        with manager.lifecycle._coordinator.exclusive("agent"):
            thread = Thread(target=wake)
            thread.start()
            self.assertTrue(started.wait(1))
            self.assertFalse(finished.wait(0.05))
        thread.join(1)

        self.assertTrue(finished.is_set())
        self.assertFalse(failures)
        self.assertEqual(len(service.wake_calls), 1)

    def test_park_still_rejects_attached_activity(self) -> None:
        service = _WakeService(self.root)
        manager = DirectNodeRuntime(service)  # type: ignore[arg-type]
        manager.lifecycle._coordinator.acquire_shared("agent")
        try:
            with self.assertRaisesRegex(SandboxBusyError, "start_agent"):
                manager.park(
                    "agent",
                    operation_id="relay-park:request-1",
                )
        finally:
            manager.lifecycle._coordinator.release_shared("agent")

        self.assertFalse(service.park_calls)

    def test_local_wake_reaches_storage_while_publication_is_pending(self) -> None:
        service = _WakeService(self.root)
        service.publication_pending = True
        manager = DirectNodeRuntime(service)  # type: ignore[arg-type]

        record, revision = manager.wake_with_activity_revision(
            "agent",
            generation=1,
            operation_id="relay-wake:request-3",
        )
        self.assertEqual(record.state, "running")
        self.assertEqual(revision, 101)
        self.assertEqual(service.wake_calls, [("agent", 1, "relay-wake:request-3")])


if __name__ == "__main__":
    unittest.main()


class RelayLifecycleFenceTests(unittest.TestCase):
    def test_wake_intent_prevents_late_park_even_when_restore_failed(self):
        from unittest.mock import Mock
        from sandboxes.sandbox import SandboxConflictError
        seen=set()
        registry=Mock()
        def fence(sandbox,generation,request,*,record=False):
            if record:
                seen.add((sandbox,generation,request))
            return (sandbox,generation,request) in seen
        registry.relay_wake_fence.side_effect=fence
        service=Mock()
        service.telemetry = None
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        service.warden.config.runtime_root = Path(directory.name) / "runsc"
        registry.path = Path(directory.name) / "registry.sqlite"
        service.provisioner.registry=registry
        service.wake.side_effect=RuntimeError('restore temporarily blocked')
        manager=DirectNodeRuntime(service)
        with self.assertRaisesRegex(RuntimeError,'temporarily blocked'):
            manager.wake_with_activity_revision('s1',generation=1,operation_id='wake:r',relay_request_id='r')
        with self.assertRaisesRegex(SandboxConflictError,'superseded'):
            manager.park_with_activity_revision('s1',generation=1,operation_id='park:r',relay_request_id='r')
        service.park.assert_not_called()
