from __future__ import annotations

import json
import logging
import os
import time
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import replace
from pathlib import Path
from threading import Event, Lock, RLock, Thread, local
from typing import BinaryIO, Iterator
from uuid import uuid4

from .direct_service import DirectSandboxService
from .exec_fence import ExecFence
from .models import ResidentWaitMetrics
from .pause_handoff import AGENT_DEMAND_FILE, RESIDENT_FILE, STATUS_FILE, DemandPublisher, NodedStatus, noded_directory
from .managed_process import (
    ManagedProcessLogChunk,
    ManagedProcessRecord,
    ManagedProcessStart,
)
from .models import ResourceQuantity
from . import phase_timings
from .sandbox import (
    OPERATION_ID_RE,
    NodeDrainSnapshot,
    NodeDrainState,
    SandboxActivitySnapshot,
    SandboxAdmissionClosedError,
    SandboxBusyError,
    SandboxConflictError,
    SandboxExecAdmissionDeferredError,
    SandboxLifecycleCoordinator,
    SandboxOperation,
    SandboxRecord,
    SandboxSpec,
    WarmParkDeferred,
    _atomic_write_json,
    compose_activity_revision,
    validate_container_path,
)
from .upload_archive import ArchivePlan, normalized_archive, plan_archive

_LOG = logging.getLogger(__name__)
# The heartbeat's resident_wait still carries warm parking's fields
# (models.ResidentWaitMetrics); nothing parks warm.
NO_WARM_PARKS = {"resident_waits": 0, "checkpoint_inflight": 0, "checkpoints_completed": 0,
                 "reclaim_target_bytes": 0, "projected_reclaim_bytes": 0, "reason": "resident_headroom",
                 "cache_reclaim_inflight": 0, "cache_reclaim_attempts": 0, "cache_reclaimed_bytes": 0,
                 "cache_refault_backoffs": 0}


class NodeStateStore:
    """Small crash-durable node state that is independent of sandbox ownership."""

    VERSION = 1

    def __init__(self, path: Path) -> None:
        if not path.is_absolute():
            raise ValueError("node state path must be absolute")
        self.path = path
        self._lock = Lock()

    def load_drain(self) -> NodeDrainState:
        with self._lock:
            if not self.path.exists():
                return NodeDrainState()
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or set(raw) != {"version", "drain"}:
                raise ValueError("node state has an invalid schema")
            if raw["version"] != self.VERSION:
                raise ValueError("node state has an unsupported version")
            return NodeDrainState.from_dict(raw["drain"])

    def save_drain(self, drain: NodeDrainState) -> None:
        with self._lock:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _atomic_write_json(
                self.path,
                {"drain": drain.to_dict(), "version": self.VERSION},
            )


class BuilderNodeRuntime:
    """Durable drain and admission state for an image-builder node."""

    def __init__(self, state_store: NodeStateStore) -> None:
        self._state_store = state_store
        self._drain_guard = RLock()
        self._drain = state_store.load_drain()

    def configure_drain(
        self,
        token: str,
        draining: bool,
        *,
        active_build_count,
    ) -> NodeDrainSnapshot:
        token = token.strip()
        if not token or not OPERATION_ID_RE.fullmatch(token):
            raise ValueError("drain token contains unsupported characters")
        with self._drain_guard:
            current = self._drain
            if current.draining and current.token != token:
                raise SandboxConflictError("node is draining with another token")
            if not draining and current.token != token:
                raise SandboxConflictError("node is not draining with this token")
            self._drain = NodeDrainState(
                draining=draining,
                token=token,
                drain_activity_epoch=0,
                admission_open=not draining,
            )
            self._state_store.save_drain(self._drain)
            return self._heartbeat_snapshot_locked(
                active_build_count=active_build_count,
            )

    @contextmanager
    def image_operation(self, image_manager):
        with self._drain_guard:
            if self._drain.draining:
                raise SandboxAdmissionClosedError("builder node admission is closed")
            operation = image_manager.image_operation()
            operation.__enter__()
        try:
            yield
        finally:
            operation.__exit__(None, None, None)

    def heartbeat_snapshot(self, *, active_build_count) -> NodeDrainSnapshot:
        with self._drain_guard:
            return self._heartbeat_snapshot_locked(
                active_build_count=active_build_count,
            )

    def _heartbeat_snapshot_locked(self, *, active_build_count) -> NodeDrainSnapshot:
        activity = SandboxActivitySnapshot(
            records=(),
            active_sandboxes=0,
            used_resources=ResourceQuantity(),
            reserved_resources=ResourceQuantity(),
            activity_revision=0,
        )
        return NodeDrainSnapshot(
            activity=activity,
            drain=self._drain,
            active_image_builds=max(0, active_build_count()),
        )


class DirectExecRuntime:
    def __init__(self, owner: DirectNodeRuntime) -> None:
        self.owner = owner

    def exec_command(
        self,
        sandbox_id: str,
        command: tuple[str, ...],
        *,
        env: dict[str, str] | None = None,
        working_dir: str | None = None,
        interactive: bool = True,
        tty: bool = False,
        user: str | None = None,
    ) -> tuple[str, ...]:
        del interactive
        if tty:
            raise ValueError("direct runtime TTY exec is not yet qualified")
        start_lock = self.owner._acquire_exec_start(sandbox_id)
        try:
            registration = self.owner.service._require_registration(sandbox_id)
            lease = self.owner.service.warden.exec_lease(
                registration.to_direct_sandbox(),
                command,
                env=env,
                working_dir=working_dir,
                user=user,
            )
            started = time.monotonic()
            argv = lease.__enter__()
            self.owner._record_exec_start_timing(
                "exec_lease",
                (time.monotonic() - started) * 1000,
            )
            try:
                self.owner._attach_exec_lease(sandbox_id, lease)
            except Exception:
                lease.__exit__(None, None, None)
                raise
            return argv
        except Exception:
            self.owner._release_exec_start_lock(sandbox_id, start_lock)
            raise

    def exec_started(self, sandbox_id: str) -> None:
        """Release the start fence once runsc exec owns the child request.

        The fence closes the build/spawn race with park and delete. Keeping it
        for the child lifetime would make forced deletion impossible: the
        runsc client only exits after deletion, while deletion would wait for
        the client to release this fence.
        """

        self.owner._release_exec_start(sandbox_id)

    def exec_start_failed(self, sandbox_id: str) -> None:
        self.owner._release_exec_start(sandbox_id)


class DirectLifecycle:
    def __init__(self, owner: DirectNodeRuntime) -> None:
        self.owner = owner
        self._coordinator = SandboxLifecycleCoordinator()
        # Each shared holder's A descriptor, per sandbox id.
        self._activity_holds: dict[str, list[int]] = {}
        self._activity_guard = Lock()

    def is_idle(self, sandbox_id: str) -> bool:
        return self._coordinator.is_idle(sandbox_id) and self.owner.exec_fence.activity_idle(sandbox_id)

    def acquire_shared(self, sandbox_id: str) -> None:
        # A parked guest can race a tool request with a local deferred park.
        # Queue before accepting exec/file activity, then re-read registration
        # and restore under its generation lock. No command has been launched,
        # so a bounded wait expiring is safe for the gateway to reschedule.
        wait_seconds = getattr(self.owner.service, "admission_wait_seconds", 30.0)
        try:
            self._coordinator.acquire_shared(
                sandbox_id,
                join_transition=True,
                transition_timeout_seconds=wait_seconds,
            )
        except SandboxBusyError as exc:
            raise SandboxExecAdmissionDeferredError(str(exc)) from exc
        try:
            self._hold_activity(sandbox_id, wait_seconds)
        except SandboxBusyError as exc:
            self._coordinator.release_shared(sandbox_id)
            raise SandboxExecAdmissionDeferredError(str(exc)) from exc
        except BaseException:
            self._coordinator.release_shared(sandbox_id)
            raise
        try:
            registration = self.owner.service._require_registration(sandbox_id)
            # The shared fence keeps park and delete out, so a running sandbox
            # needs no request lock: holding it for every command serialized a
            # sandbox's commands behind each other's inspection.
            timings = self.owner.service.running_timings(registration.to_direct_sandbox())
            if timings is not None:
                self.owner.service.mark_activity(sandbox_id, registration.sandbox_generation)
            else:
                # Concurrent commands for one sandbox queue here for the same
                # bounded wait. Rejecting them made each one retry a second later.
                with self.owner.service._request_lock(
                    sandbox_id,
                    registration.sandbox_generation,
                    wait_seconds=wait_seconds,
                ):
                    self.owner.service.mark_activity(
                        sandbox_id,
                        registration.sandbox_generation,
                    )
                    timings = self.owner.service.ensure_running_with_timings(
                        registration.to_direct_sandbox()
                    )
            self.owner._set_exec_start_timings(timings)
        except Exception:
            self._release_activity(sandbox_id)
            self._coordinator.release_shared(sandbox_id)
            raise

    def release_shared(self, sandbox_id: str) -> None:
        try:
            registration = self.owner.service.provisioner.registry.get(sandbox_id)
            if registration is not None:
                self.owner.service.mark_activity(
                    sandbox_id,
                    registration.sandbox_generation,
                )
        finally:
            try:
                self._release_activity(sandbox_id)
            finally:
                self._coordinator.release_shared(sandbox_id)

    def _hold_activity(self, sandbox_id: str, wait_seconds: float) -> None:
        """Hold A shared until release_shared, so a noded pause cannot land
        between this op's spawn and its completion. Delete may unlink A
        meanwhile; the descriptor then fences nothing new."""
        if self.owner.service.provisioner.registry.get(sandbox_id) is None:
            return  # No fence files for an unknown id: the registration check refuses it next.
        activity = self.owner.exec_fence.hold_activity(sandbox_id, timeout_seconds=wait_seconds)
        with self._activity_guard:
            self._activity_holds.setdefault(sandbox_id, []).append(activity)

    def _release_activity(self, sandbox_id: str) -> None:
        with self._activity_guard:
            held = self._activity_holds.get(sandbox_id)
            descriptor = held.pop() if held else None
            if held is not None and not held:
                del self._activity_holds[sandbox_id]
        if descriptor is not None:
            os.close(descriptor)

    @contextmanager
    def shared(self, sandbox_id: str) -> Iterator[None]:
        self.acquire_shared(sandbox_id)
        try:
            yield
        finally:
            self.release_shared(sandbox_id)

    @contextmanager
    def exclusive(
        self,
        sandbox_id: str,
        *,
        allow_shared: bool = False,
        join_transition: bool = False,
        transition_timeout_seconds: float | None = None,
    ) -> Iterator[None]:
        with self._coordinator.exclusive(
            sandbox_id,
            allow_shared=allow_shared,
            join_transition=join_transition,
            transition_timeout_seconds=transition_timeout_seconds,
        ):
            # Every lifecycle transition passes here, so noded's execs (which
            # the coordinator cannot see) are fenced by the same rules.
            with self.owner.exec_fence.transition(sandbox_id, allow_shared=allow_shared):
                yield

    def discard_fence(self, sandbox_id: str) -> None:
        """Unlink a deleted sandbox's exec fence files, inside its exclusive."""
        self.owner.exec_fence.discard(sandbox_id)


class DirectNodeRuntime:
    """Node-level orchestration for the direct sandbox service.

    runtime/noded runs execs on running, unpaused sandboxes; lifecycle
    transitions fence them through flock files and their activity clock
    (sandboxes/exec_fence.py). noded also pauses and thaws, runs idle pauses,
    local waits, paused reclaim and decides escalations (pause_handoff.py):
    this agent executes the escalations noded asks for and publishes the
    demand paused reclaim bills.
    """

    def __init__(
        self,
        service: DirectSandboxService,
        *,
        rust_admission: bool = False,
    ) -> None:
        self.service = service
        self.exec_fence = service.exec_fence = ExecFence(
            service.warden.config.runtime_root / "warden-locks")
        # --rust-admission (step 3c.3): noded holds the node's ledger, which is
        # then the reclaim demand.
        noded_admission = getattr(getattr(service, "admission", None), "remote", False)
        directory = noded_directory(service)
        self._demand_publisher = (None if noded_admission
                                  else DemandPublisher(service, directory / AGENT_DEMAND_FILE))
        self._noded_status = NodedStatus(directory / STATUS_FILE)
        # --rust-admission, phase 3b so far (docs/architecture.md): noded samples managed sandboxes; every resident reader here
        # and the growth forecasts read its resident.json instead.
        self.rust_admission = bool(rust_admission)
        if rust_admission:
            service.use_published_resident_memory(directory / RESIDENT_FILE)
        self.lifecycle = DirectLifecycle(self)
        self.runtime = DirectExecRuntime(self)
        self._exec_leases: dict[str, object] = {}
        self._exec_start_locks: dict[str, Lock] = {}
        self._exec_start_users: dict[str, int] = {}
        self._activity_guard = Lock()
        self._exec_start_state = local()
        self._drain_guard = RLock()
        self._registry = service.provisioner.registry
        self._drain = self._registry.load_drain()
        if noded_admission:
            # noded reads the drain row itself; a noded that is not up yet
            # follows this agent's gate at its hello.
            if self._drain.draining:
                service.admission.close(self._drain.token, strict=False)
            else:
                service.admission.open(self._drain.token or None, strict=False)
        elif self._drain.draining:
            self.service.close_admission()
        else:
            self.service.open_admission()
        self._background_stop = Event()
        self._sampling_thread: Thread | None = None

    def start(self) -> None:
        self._background_stop.clear()
        if self._demand_publisher is not None:
            self._demand_publisher.start()
        if self._sampling_thread is None or not self._sampling_thread.is_alive():
            self._sampling_thread = Thread(
                target=self._sampling_loop,
                name="ucloud-direct-resident-sampler", daemon=True,
            )
            self._sampling_thread.start()

    def record_growth_events(self, items) -> list[str | None]:
        """noded's batched local waits and answers (POST /internal/v1/growth/events)."""
        def one(method, key, request_id):
            try:
                getattr(self.service, method)(key[0], key[1], request_id)
            except Exception as exc:  # noqa: BLE001
                return exc
        batch = getattr(self.service, "record_local_wait_growth", None)
        try:
            errors = batch(items) if batch is not None else [one(*item) for item in items]
        except Exception as exc:  # noqa: BLE001
            errors = [exc] * len(items)
        for (_, key, _), error in zip(items, errors):
            if isinstance(error, Exception):
                _LOG.warning("local wait growth bookkeeping for %s skipped: %s", key[0], error)
        return [str(error) if isinstance(error, Exception) else None for error in errors]

    def stop(self) -> None:
        self._background_stop.set()
        if self._demand_publisher is not None:
            self._demand_publisher.stop()
        thread = self._sampling_thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._sampling_thread = None

    def resident_wait_snapshot(self):
        warden = self.service.warden
        own = warden.pause_stats.snapshot()
        snapshot = {**NO_WARM_PARKS, **self.service.resident_demand_snapshot(), **own,
                    "paused_sandboxes": len(warden.paused_keys())}
        # noded's pauses, thaws and reclaims add to this agent's transition
        # thaws; an invalid sum keeps this agent's own.
        composed = {**snapshot, **self._noded_status.compose(own)}
        return composed if ResidentWaitMetrics.from_dict(composed) is not None else snapshot

    def _sampling_loop(self) -> None:
        next_sample = 0.0
        while not self._background_stop.wait(0.25):
            # --rust-admission: noded samples; each tick looks for its next pass.
            if self.rust_admission or time.monotonic() >= next_sample:
                try:
                    sampler = getattr(self.service, "refresh_resident_memory", None)
                    if sampler is not None:
                        sampler()
                except (OSError, RuntimeError, ValueError):
                    pass  # Missing observations carry no growth credit.
                next_sample = time.monotonic() + 1.0

    def escalate_pause(self, sandbox_id: str, generation: int) -> SandboxRecord:
        """Hibernate a paused sandbox; noded decided it must.

        SandboxConflictError (lost to activity: thawed, busy or gone) and
        WarmParkDeferred (no capture space: it stays paused) as from park.
        This agent counts ``pause_escalations``, for noded's too.
        """
        record, _ = self.park_with_activity_revision(
            sandbox_id, operation_id=f"pause-escalation:{uuid4().hex}",
            generation=generation, escalate=True)
        if record.state == "parked":
            self.service.warden.pause_stats.add(pause_escalations=1)
        return record

    def create_with_timings(
        self,
        spec: SandboxSpec,
        *,
        operation: SandboxOperation,
    ) -> tuple[SandboxRecord, dict[str, object]]:
        existing = self.service.get_snapshot(spec.id)
        started = time.monotonic()
        with phase_timings.recording() as phases:
            record = self.service.create(spec, operation=operation)
        return (
            record,
            {
                "idempotent": existing is not None and existing == record,
                "total_ms": max(0, int((time.monotonic() - started) * 1000)),
                # Nested and repeated phases; not additive. See phase_timings.
                "phases": dict(phases),
            },
        )

    def delete(
        self,
        sandbox_id: str,
        *,
        generation: int,
        operation_id: str,
    ) -> SandboxRecord | None:
        if generation <= 0:
            raise ValueError("delete generation must be positive")
        if not isinstance(operation_id, str) or not OPERATION_ID_RE.fullmatch(
            operation_id
        ):
            raise ValueError("delete operation id is invalid")
        record = self.service.get(sandbox_id)
        if record is not None and record.generation != generation:
            raise SandboxConflictError("delete generation does not own direct sandbox")
        # Deletion is a hard revocation boundary. It closes new activity but is
        # allowed to sever attached exec sessions during sandbox deletion. It
        # waits for an in-flight park or wake (an idle park can outlast the
        # caller's next request on a busy node) instead of failing with 409.
        with self.lifecycle.exclusive(
            sandbox_id,
            allow_shared=True,
            join_transition=True,
            transition_timeout_seconds=60.0,
        ):
            self.service.delete(
                sandbox_id,
                generation=generation,
            )
            self.lifecycle.discard_fence(sandbox_id)
        return record

    def get(self, sandbox_id: str) -> SandboxRecord | None:
        return self.service.get(sandbox_id)

    def list(self) -> list[SandboxRecord]:
        self.cleanup_expired(blocking=False)
        return list(self.service.list_snapshot())

    def park(
        self,
        sandbox_id: str,
        *,
        operation_id: str,
        background: bool = False,
    ) -> SandboxRecord:
        record, _activity_revision = self.park_with_activity_revision(
            sandbox_id,
            operation_id=operation_id,
            background=background,
        )
        return record

    def park_with_activity_revision(
        self,
        sandbox_id: str,
        *,
        operation_id: str,
        background: bool = False,
        relay_request_id: str | None = None,
        generation: int | None = None,
        escalate: bool = False,
    ) -> tuple[SandboxRecord, int]:
        if not isinstance(operation_id, str) or not OPERATION_ID_RE.fullmatch(
            operation_id
        ):
            raise ValueError("park operation id is invalid")
        if relay_request_id is not None and generation is None:
            raise ValueError("relay park requires generation")
        if relay_request_id is not None and self.service.provisioner.registry.relay_wake_fence(
            sandbox_id, generation, relay_request_id,
        ):
            raise SandboxConflictError("relay park was superseded by durable wake")
        if relay_request_id is not None:
            observe_wait = getattr(self.service, "observe_managed_wait", None)
            if observe_wait is not None:
                observe_wait(sandbox_id, generation, relay_request_id)
        # The pause tier: a model wait pauses in place; an explicit API park
        # (durable park, drain and offload moves) hibernates now.
        pause = relay_request_id is not None
        try:
            # Join a concurrent park/wake and then re-evaluate the stable
            # runtime state. This makes exact replays and crossed lifecycle
            # calls idempotent without weakening the attached-activity fence.
            with self.lifecycle.exclusive(
                sandbox_id,
                join_transition=True,
                transition_timeout_seconds=60.0,
            ):
                if escalate and not self.service.warden.is_paused(sandbox_id, generation):
                    raise SandboxConflictError("pause escalation lost to activity")
                if relay_request_id is not None and self.service.provisioner.registry.relay_wake_fence(
                        sandbox_id, generation, relay_request_id):
                    raise SandboxConflictError("relay park was superseded by durable wake")
                record = self.service.park(sandbox_id, operation_id=operation_id,
                                           background=background, pause=pause)
                activity_revision = self.service.advance_lifecycle_activity_revision()
                return record, activity_revision
        except WarmParkDeferred as exc:
            # No capture space: callers retry after 30 s, not the capture's 5 s.
            thread = self._sampling_thread
            if thread is not None and thread.is_alive() and not self._background_stop.is_set():
                raise WarmParkDeferred(30.0) from exc
            raise
        except SandboxBusyError as exc:
            raise SandboxBusyError(
                "sandbox has active exec/file activity that cannot survive park: "
                f"{sandbox_id}; launch a long-lived agent in a managed_process "
                "sandbox through the SDK start_agent() API"
            ) from exc

    def wake(
        self,
        sandbox_id: str,
        *,
        generation: int,
        operation_id: str,
    ) -> SandboxRecord:
        record, _activity_revision = self.wake_with_activity_revision(
            sandbox_id,
            generation=generation,
            operation_id=operation_id,
        )
        return record

    def wake_with_activity_revision(
        self,
        sandbox_id: str,
        *,
        generation: int,
        operation_id: str,
        relay_request_id: str | None = None,
    ) -> tuple[SandboxRecord, int]:
        if generation <= 0:
            raise ValueError("wake generation must be positive")
        if not isinstance(operation_id, str) or not OPERATION_ID_RE.fullmatch(
            operation_id
        ):
            raise ValueError("wake operation id is invalid")
        # Keep the current safe wait reclaimable until growth is admitted.
        # Cancelling/fencing every wait before a response burst obtains memory
        # would leave only non-reclaimable queued continuations on a full node.
        if relay_request_id is not None:
            continuation = getattr(self.service, "admit_managed_continuation", None)
            if continuation is not None:
                telemetry = getattr(self.service, "telemetry", None)
                with (telemetry.span("sandbox.wake.growth_admission")
                      if telemetry is not None else nullcontext()):
                    continuation(sandbox_id, generation, relay_request_id)
        # Waking an already-running sandbox is a successful no-op. Attached
        # activity is proof that the current runtime is live, not a reason to
        # reject that idempotent result. We still take the exclusive transition
        # fence so a concurrent park completes first and is then re-evaluated.
        with ExitStack() as lifecycle:
            telemetry = getattr(self.service, "telemetry", None)
            with (telemetry.span("sandbox.wake.lifecycle_wait")
                  if telemetry is not None else nullcontext()):
                lifecycle.enter_context(self.lifecycle.exclusive(
                    sandbox_id,
                    allow_shared=True,
                    join_transition=True,
                    transition_timeout_seconds=60.0,
                ))
            if relay_request_id is not None:
                self.service.provisioner.registry.relay_wake_fence(
                    sandbox_id, generation, relay_request_id, record=True,
                )
            # Local wake takes precedence over background publication. The
            # storage journal supersedes/fences the upload while retaining the
            # sealed checkpoint; an uploader thread is not an admission limit.
            record = self.service.wake(
                sandbox_id,
                generation=generation,
                operation_id=operation_id,
            )
            activity_revision = self.service.advance_lifecycle_activity_revision()
            return record, activity_revision

    def start_managed_process(
        self,
        sandbox_id: str,
        spec: ManagedProcessStart,
    ) -> ManagedProcessRecord:
        with self.lifecycle.shared(sandbox_id):
            return self.service.start_managed_process(sandbox_id, spec)

    def admit_managed_launch(self, sandbox_id: str, generation: int, job_id: str, launch_sha256: str):
        """POST /internal/v1/managed/launch, under the managed routes' fence."""
        with self.lifecycle.shared(sandbox_id):
            return self.service.admit_managed_launch(sandbox_id, generation, job_id, launch_sha256)

    def bind_managed_launch(self, sandbox_id: str, generation: int, job_id: str, launch_sha256: str):
        """POST /internal/v1/managed/bind."""
        with self.lifecycle.shared(sandbox_id):
            return self.service.bind_managed_launch(sandbox_id, generation, job_id, launch_sha256)

    def observe_managed_terminal(self, sandbox_id: str, generation: int, job_id: str):
        """POST /internal/v1/managed/terminal."""
        with self.lifecycle.shared(sandbox_id):
            return self.service.observe_managed_terminal(sandbox_id, generation, job_id)

    def managed_process_status(
        self,
        sandbox_id: str,
        job_id: str,
    ) -> ManagedProcessRecord:
        with self.lifecycle.shared(sandbox_id):
            return self.service.managed_process_status(sandbox_id, job_id)

    def managed_process_logs(
        self,
        sandbox_id: str,
        job_id: str,
        *,
        stream: str,
        offset: int,
        limit: int,
    ) -> ManagedProcessLogChunk:
        with self.lifecycle.shared(sandbox_id):
            return self.service.managed_process_logs(
                sandbox_id,
                job_id,
                stream=stream,
                offset=offset,
                limit=limit,
            )

    def signal_managed_process(
        self,
        sandbox_id: str,
        job_id: str,
        *,
        signal: int,
    ) -> ManagedProcessRecord:
        with self.lifecycle.shared(sandbox_id):
            return self.service.signal_managed_process(
                sandbox_id,
                job_id,
                signal=signal,
            )

    def require_activity_sandbox(self, sandbox_id: str) -> SandboxRecord:
        record = self.service.get(sandbox_id)
        if record is None:
            raise ValueError(f"sandbox not found: {sandbox_id}")
        return record

    def acquire_exec_capacity(self, sandbox_id: str) -> str:
        registration = self.service._require_registration(sandbox_id)
        return self.service.acquire_exec_capacity(
            sandbox_id,
            registration.sandbox_generation,
        )

    def release_exec_capacity(self, token: str) -> None:
        self.service.release_exec_capacity(token)

    def consume_exec_start_timings(self) -> dict[str, float]:
        timings = dict(getattr(self._exec_start_state, "timings", {}))
        self._exec_start_state.timings = {}
        return timings

    def _set_exec_start_timings(self, timings: dict[str, float]) -> None:
        self._exec_start_state.timings = dict(timings)

    def _record_exec_start_timing(self, name: str, value: float) -> None:
        timings = dict(getattr(self._exec_start_state, "timings", {}))
        timings[name] = value
        self._exec_start_state.timings = timings

    def upload_file(
        self,
        sandbox_id: str,
        path: str,
        content: bytes,
        *,
        expected_generation: int | None = None,
    ) -> None:
        # The exec fence, not the request lock: one sandbox's harness files
        # upload concurrently, where the lock refused all but one at a time.
        with self._file_fence(sandbox_id, expected_generation):
            self.service.write_file(sandbox_id, path, content, expected_generation=expected_generation, fenced=True)

    def upload_file_from_file(
        self, sandbox_id: str, path: str, source: BinaryIO, size: int,
        *, expected_generation: int,
    ) -> None:
        with self._file_fence(sandbox_id, expected_generation):
            self.service.write_file_from_file(
                sandbox_id, path, source, size, expected_generation=expected_generation, fenced=True,
            )

    def upload_archive(
        self, sandbox_id: str, directory: str, source: BinaryIO, *,
        expected_generation: int, max_bytes: int,
    ) -> ArchivePlan:
        """Validate the whole archive, then extract it with one fenced exec."""
        validate_container_path("sandbox archive directory", directory)
        plan = plan_archive(source, max_bytes=max_bytes)
        with normalized_archive(source, plan, self.service.upload_spool) as stdin:
            with self._file_fence(sandbox_id, expected_generation):
                self.service.extract_archive(
                    sandbox_id, directory, plan, expected_generation=expected_generation, fenced=True, **stdin,
                )
        return plan

    @contextmanager
    def _file_fence(self, sandbox_id: str, expected_generation: int | None):
        """The shared lifecycle fence, after the generation check: a write whose
        sandbox was replaced while its body arrived answers a conflict."""
        if expected_generation is not None and (
            self.service._require_registration(sandbox_id).sandbox_generation != expected_generation
        ):
            raise SandboxConflictError("file operation no longer owns direct sandbox generation")
        self.lifecycle.acquire_shared(sandbox_id)
        try:
            yield
        finally:
            self.lifecycle.release_shared(sandbox_id)

    def download_file(
        self,
        sandbox_id: str,
        path: str,
        *,
        max_bytes: int,
    ) -> bytes:
        return self.service.read_file(sandbox_id, path, max_bytes=max_bytes)

    def cleanup_expired(self, *, blocking: bool = True) -> list[SandboxRecord]:
        records = self.service.list() if blocking else self.service.list_snapshot()
        return self._cleanup_expired_records(records, blocking=blocking)

    def _cleanup_expired_records(
        self,
        records: tuple[SandboxRecord, ...] | list[SandboxRecord],
        *,
        blocking: bool,
    ) -> list[SandboxRecord]:
        expired = [record for record in records if record.is_expired()]
        for record in expired:
            if blocking:
                self.service.delete(record.spec.id, generation=record.generation)
            else:
                self.service.try_delete(
                    record.spec.id,
                    generation=record.generation,
                )
        return expired

    def configure_drain(
        self,
        token: str,
        draining: bool,
        *,
        active_build_count,
    ) -> NodeDrainSnapshot:
        token = token.strip()
        if not token or not OPERATION_ID_RE.fullmatch(token):
            raise ValueError("drain token contains unsupported characters")
        with self._drain_guard:
            current = self._drain
            if draining:
                if current.draining and current.token != token:
                    raise SandboxConflictError("node is draining with another token")
                if not current.draining:
                    # Synchronous, before the drain row: noded's admitted
                    # creates are in its snapshot once this returns (I18).
                    self.service.close_admission(token)
                    self._drain = NodeDrainState(
                        draining=True,
                        token=token,
                        drain_activity_epoch=0,
                        admission_open=False,
                    )
                    self._registry.save_drain(self._drain)
            else:
                if current.draining and current.token != token:
                    raise SandboxConflictError("node is not draining with this token")
                if not current.draining and current.token != token:
                    raise SandboxConflictError("node is not draining with this token")
                if current.draining:
                    self.service.open_admission(token)
                    self._drain = NodeDrainState(
                        draining=False,
                        token=token,
                        admission_open=True,
                    )
                    self._registry.save_drain(self._drain)
        return self.heartbeat_snapshot(active_build_count=active_build_count)

    @contextmanager
    def image_operation(self, image_manager):
        """Admit image pull/materialization atomically against node drain."""

        with self._drain_guard:
            if self._drain.draining or not self.service.admission_open:
                raise SandboxAdmissionClosedError("direct node admission is closed")
            operation = image_manager.image_operation()
            operation.__enter__()
        try:
            yield
        finally:
            operation.__exit__(None, None, None)

    def heartbeat_snapshot(self, *, active_build_count) -> NodeDrainSnapshot:
        # Drain admission and the empty proof share this lock. A heartbeat
        # that began just before drain therefore cannot publish an empty proof
        # from observations taken before admission closed.
        with self._drain_guard:
            snapshot = self._heartbeat_snapshot_locked(
                active_build_count=active_build_count,
            )
        # Expiry is deliberately reconciled after publishing this conservative
        # snapshot. Deleting before the empty proof would require a second
        # registry parse; counting an expired sandbox for one extra heartbeat is
        # safe, while publishing an inventory assembled before drain admission
        # closed is not.
        self._cleanup_expired_records(snapshot.activity.records, blocking=False)
        return snapshot

    def _heartbeat_snapshot_locked(self, *, active_build_count) -> NodeDrainSnapshot:
        # Read transient operations before durable inventory. During drain,
        # admission has already been atomically closed, so this ordering cannot
        # miss a create transitioning from pre-registry work into the registry.
        transient = self.service.activity_snapshot()
        inventory = self.service.inventory_snapshot()
        noded_session = getattr(transient, "noded_session", None)
        if noded_session is not None:
            # The transient half is noded's (§3.6): a noded restarted between
            # it and the inventory took its claims along. Skip this sample.
            self.service.admission.confirm_session(noded_session)
        records = inventory.records
        registered_keys = {(record.spec.id, record.generation) for record in records}
        direct_sandboxes = tuple(
            item.registration.to_direct_sandbox()
            for item in inventory.items
            if item.registration.has_direct_sandbox
        )
        storage_records = self.service.warden.storage_records_snapshot(direct_sandboxes)
        storage_dependencies: dict[str, dict] = {}
        used = ResourceQuantity()
        reserved = ResourceQuantity()
        # The registry's per-registration claims are the admission authority:
        # dynamic claims follow demonstrated usage (docs/architecture.md) and
        # already exclude a published workspace.
        claims = self._registry.disk_claims_mb()
        for item in inventory.items:
            record = item.record
            registration = item.registration
            quota_disk = (
                registration.quota_total_mb
                if registration.quota_total_mb is not None
                else record.spec.disk_mb or 0
            )
            storage_dependencies[record.spec.id] = {}
            claim = claims.get((registration.sandbox_id, registration.sandbox_generation))
            charged_disk_mb = quota_disk if claim is None else claim
            # Planned and quota-ready registrations are valid, durable create
            # reservations but do not own a runsc sandbox yet. They remain
            # visible in heartbeat capacity accounting while a cold image is
            # materialized.
            if registration.has_direct_sandbox:
                storage = storage_records[registration.workspace_volume_id]
                if claim is None and storage.state.value == "published":
                    # Publication releases the workspace volume only. Split
                    # checkpoint memory retains its local quota until deletion.
                    memory = registration.memory_reference
                    charged_disk_mb = ((memory.quota_bytes + 1024**2 - 1) // 1024**2
                                       if memory is not None else 0)
                if storage.published_layers:
                    storage_dependencies[record.spec.id] = (
                        storage.dependency_publication().to_dict()
                    )
            resources = ResourceQuantity(disk_mb=charged_disk_mb)
            if record.state == "running":
                # Direct-runtime CPU and memory limits bound an individual
                # sandbox; they are not permanent node reservations. Actual
                # host consumption and pressure are reported separately in
                # runtime_metrics. Disk remains additive and hard.
                used = used + resources
            elif record.state not in {"parked"}:
                reserved = reserved + ResourceQuantity(
                    vcpu=record.spec.cpus or 0,
                    memory_mb=record.spec.memory_mb or 0,
                    disk_mb=charged_disk_mb,
                )
            else:
                used = used + resources
        for key, resources in transient.resource_reservations.items():
            if key in registered_keys:
                continue
            # The gateway route owns the exact hard-disk reservation before a
            # node registry record exists. Only transient CPU/RAM are added
            # here, avoiding duplicate disk charging during placement.
            reserved = reserved + ResourceQuantity(
                vcpu=resources.vcpu,
                memory_mb=resources.memory_mb,
            )
        revision = compose_activity_revision(
            durable_revision=inventory.activity_revision,
            transient_revision=transient.activity_revision,
        )
        activity = SandboxActivitySnapshot(
            records=records,
            active_sandboxes=sum(record.state == "running" for record in records),
            used_resources=used,
            reserved_resources=reserved,
            activity_revision=revision,
            active_operations=transient.active_operations,
            active_sandbox_creates=transient.active_sandbox_creates,
        )
        build_count = max(0, active_build_count())
        drain = self._drain
        # noded's execs are not in active_operations. The
        # empty proof still holds: each runs in a registered sandbox, so it
        # needs a record, and noded re-reads the drain row before every start.
        # Only the exiting runsc client of a just-deleted sandbox can outlive it.
        if (
            drain.draining
            and not records
            and activity.active_operations == 0
            and build_count == 0
            and drain.drain_activity_epoch != revision
        ):
            drain = replace(drain, drain_activity_epoch=revision)
            self._drain = drain
            self._registry.save_drain(drain)
        return NodeDrainSnapshot(activity, drain, build_count, storage_dependencies)

    def _attach_exec_lease(self, sandbox_id: str, lease: object) -> None:
        with self._activity_guard:
            if sandbox_id in self._exec_leases:
                raise RuntimeError("direct sandbox already has an exec lease")
            self._exec_leases[sandbox_id] = lease

    def _acquire_exec_start(self, sandbox_id: str) -> Lock:
        with self._activity_guard:
            lock = self._exec_start_locks.setdefault(sandbox_id, Lock())
            self._exec_start_users[sandbox_id] = (
                self._exec_start_users.get(sandbox_id, 0) + 1
            )
        lock.acquire()
        return lock

    def _release_exec_start_lock(self, sandbox_id: str, lock: Lock) -> None:
        lock.release()
        with self._activity_guard:
            users = self._exec_start_users.get(sandbox_id, 0) - 1
            if users <= 0:
                self._exec_start_users.pop(sandbox_id, None)
                if self._exec_start_locks.get(sandbox_id) is lock:
                    self._exec_start_locks.pop(sandbox_id, None)
            else:
                self._exec_start_users[sandbox_id] = users

    def _pop_exec_lease(self, sandbox_id: str):
        with self._activity_guard:
            return self._exec_leases.pop(sandbox_id, None)

    def _release_exec_start(self, sandbox_id: str) -> None:
        lease = self._pop_exec_lease(sandbox_id)
        if lease is None:
            return
        with self._activity_guard:
            lock = self._exec_start_locks.get(sandbox_id)
        if lock is None:
            raise RuntimeError("direct exec start lock is unavailable")
        try:
            lease.__exit__(None, None, None)
        finally:
            self._release_exec_start_lock(sandbox_id, lock)
