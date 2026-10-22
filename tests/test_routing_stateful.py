from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from hypothesis import settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from sandboxes.models import ResourceQuantity
from sandboxes.routing import (
    SandboxRoute,
    SandboxRouteAllocation,
    open_routing_store,
)
from tests.support import (
    POSTGRES_DSN,
    POSTGRES_SKIP_REASON,
    create_routing_file,
    drop_routing_schema,
    reopen_routing_store,
)

TEST_TIER = "contract"


class RoutingStoreStateMachine(RuleBasedStateMachine):
    """Exercise one reused sandbox id against a small durable route model."""

    sandbox_id = "stateful-sandbox"

    def __init__(self) -> None:
        super().__init__()
        self._temporary_directory = TemporaryDirectory()
        self.path = Path(self._temporary_directory.name) / "routes.sqlite"
        self.schema = create_routing_file(self.path)
        self.store = open_routing_store(self.path)
        self.current: SandboxRoute | None = None
        self.current_allocation: SandboxRouteAllocation | None = None
        self.retired: list[SandboxRoute] = []
        self.high_water_generation = 0

    def teardown(self) -> None:
        drop_routing_schema(self.path, self.schema)
        self._temporary_directory.cleanup()

    @rule()
    @precondition(lambda self: self.current is None)
    def allocate(self) -> None:
        next_generation = self.high_water_generation + 1
        allocation = SandboxRouteAllocation(
            sandbox_id=self.sandbox_id,
            node_id=f"node-{next_generation}",
            job_id=f"job-{next_generation}",
            node_url=f"http://node-{next_generation}:8090",
            resources=ResourceQuantity(
                vcpu=1,
                memory_mb=512,
                disk_mb=1024,
            ),
            spec={"id": self.sandbox_id, "image": "busybox"},
        )
        route, pending = self.store.allocate_sandbox_create_with_pending(
            allocation,
            spec_hash="a" * 64,
            create_operation_id=f"create-{next_generation}",
        )

        assert pending is None
        assert route.generation == next_generation
        assert route.create_operation_id == f"create-{next_generation}"
        self.current = route
        self.current_allocation = allocation
        self.high_water_generation = route.generation

    @rule()
    @precondition(lambda self: self.current is not None)
    def replay_allocation_after_reopen(self) -> None:
        assert self.current is not None
        assert self.current_allocation is not None
        self.store = reopen_routing_store(self.path)

        replay, pending = self.store.allocate_sandbox_create_with_pending(
            self.current_allocation,
            spec_hash=self.current.spec_hash,
            create_operation_id=self.current.create_operation_id,
        )

        assert pending is None
        assert replay == self.current

    @rule(state=st.sampled_from(("creating", "running", "parked")))
    @precondition(
        lambda self: self.current is not None and not self.current.delete_operation_id
    )
    def update_state(self, state: str) -> None:
        assert self.current is not None
        updated = self.store.set_sandbox_state_if_current(
            self.current,
            expected_states={self.current.state},
            state=state,
        )

        assert updated is not None
        assert updated.generation == self.current.generation
        assert updated.create_operation_id == self.current.create_operation_id
        self.current = updated

    @rule()
    @precondition(
        lambda self: self.current is not None and not self.current.delete_operation_id
    )
    def prepare_delete_is_replay_safe_across_reopen(self) -> None:
        prepared = self.store.prepare_sandbox_delete(self.sandbox_id)
        assert prepared is not None
        self.store = reopen_routing_store(self.path)

        replay = self.store.prepare_sandbox_delete(self.sandbox_id)

        assert replay == prepared
        self.current = replay

    @rule()
    @precondition(lambda self: self.current is not None)
    def delete_current_and_reopen(self) -> None:
        assert self.current is not None
        deleted = self.store.delete_sandbox_if_current(
            self.sandbox_id,
            generation=self.current.generation,
            create_operation_id=self.current.create_operation_id,
            delete_operation_id=self.current.delete_operation_id,
        )

        assert deleted == self.current
        self.retired.append(self.current)
        self.current = None
        self.current_allocation = None
        self.store = reopen_routing_store(self.path)
        assert self.store.get_sandbox_readonly(self.sandbox_id) is None
        assert (
            self.store.delete_sandbox_if_current(
                self.sandbox_id,
                generation=deleted.generation,
                create_operation_id=deleted.create_operation_id,
                delete_operation_id=deleted.delete_operation_id,
            )
            is None
        )

    @rule()
    @precondition(lambda self: bool(self.retired))
    def reject_retired_incarnation(self) -> None:
        retired = self.retired[-1]

        assert (
            self.store.set_sandbox_state_if_current(
                retired,
                expected_states={retired.state},
                state="running",
            )
            is None
        )
        assert (
            self.store.delete_sandbox_if_current(
                self.sandbox_id,
                generation=retired.generation,
                create_operation_id=retired.create_operation_id,
                delete_operation_id=retired.delete_operation_id,
            )
            is None
        )

    @rule()
    @precondition(lambda self: self.current is not None)
    def reject_wrong_generation_and_identity(self) -> None:
        assert self.current is not None
        wrong_generation = replace(
            self.current,
            generation=self.current.generation + 1,
        )
        wrong_identity = replace(
            self.current,
            create_operation_id=f"{self.current.create_operation_id}-stale",
        )

        for stale in (wrong_generation, wrong_identity):
            assert (
                self.store.set_sandbox_state_if_current(
                    stale,
                    expected_states={self.current.state},
                    state="running",
                )
                is None
            )
        assert (
            self.store.delete_sandbox_if_current(
                self.sandbox_id,
                generation=self.current.generation + 1,
                create_operation_id=self.current.create_operation_id,
            )
            is None
        )
        assert (
            self.store.delete_sandbox_if_current(
                self.sandbox_id,
                generation=self.current.generation,
                create_operation_id=f"{self.current.create_operation_id}-stale",
            )
            is None
        )

    @rule()
    def reopen(self) -> None:
        self.store = reopen_routing_store(self.path)

    @invariant()
    def durable_state_matches_model(self) -> None:
        # Every read is a fresh PostgreSQL query; the reopen rules restart the store.
        stored = self.store.get_sandbox_readonly(self.sandbox_id)
        assert stored == self.current
        if stored is not None:
            assert stored.generation == self.high_water_generation
        assert all(
            route.generation <= self.high_water_generation for route in self.retired
        )


TestRoutingStoreStateMachine = unittest.skipUnless(POSTGRES_DSN, POSTGRES_SKIP_REASON)(
    RoutingStoreStateMachine.TestCase
)
TestRoutingStoreStateMachine.settings = settings(
    max_examples=20,
    stateful_step_count=15,
    deadline=None,
    derandomize=True,
)
