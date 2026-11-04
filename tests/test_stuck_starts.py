from datetime import timedelta
from pathlib import Path
import tempfile
import unittest

from hypothesis import given, strategies as st

from sandboxes.autoscaler_state import AutoscalerStateStore
from sandboxes.models import (
    InstancePhase,
    NodeHeartbeat,
    ProviderInstance,
    ResourceQuantity,
    SandboxInventoryEntry,
    SandboxNode,
    utc_now,
)
from sandboxes.routing import SandboxRoute
from sandboxes.stuck_starts import (
    STUCK_START_MIN_AGE,
    StuckStart,
    stuck_start_candidates,
    stuck_starts_due,
)


def entry(sandbox_id: str, generation: int = 1, state: str = "planned") -> SandboxInventoryEntry:
    return SandboxInventoryEntry(
        sandbox_id=sandbox_id, generation=generation, operation_id=f"create-{sandbox_id}-{generation}",
        spec_hash="0" * 64, state=state,
    )


def worker(
    job_id: str, inventory: tuple[SandboxInventoryEntry, ...], *, fresh: bool = True,
    complete: bool = True,
) -> SandboxNode:
    return SandboxNode(
        job=ProviderInstance(
            id=job_id, name=f"node-{job_id}", application_name="vm", application_version="1",
            product_id="ccx63", product_category="ccx", state="RUNNING", phase=InstancePhase.RUNNING,
        ),
        heartbeat=NodeHeartbeat(
            node_id=f"node-{job_id}", job_id=job_id, updated_at=utc_now(), active_sandboxes=0,
            node_url=f"http://node-{job_id}:8090", inventory=inventory, inventory_complete=complete,
        ),
        active_sandboxes=len(inventory),
        heartbeat_fresh=fresh,
    )


def route(sandbox_id: str, generation: int, job_id: str = "a") -> SandboxRoute:
    return SandboxRoute(
        sandbox_id=sandbox_id, node_id=f"node-{job_id}", job_id=job_id,
        node_url=f"http://node-{job_id}:8090", resources=ResourceQuantity(), spec={"id": sandbox_id},
        state="running", generation=generation, create_operation_id=f"create-{sandbox_id}",
        spec_hash="0" * 64,
    )


def stuck(sandbox_id: str, job_id: str = "a") -> StuckStart:
    return StuckStart(
        job_id=job_id, node_url=f"http://node-{job_id}:8090", sandbox_id=sandbox_id,
        generation=1, operation_id=f"create-{sandbox_id}-1",
    )


class StuckStartCandidateTests(unittest.TestCase):
    def test_a_planned_registration_no_route_owns_is_a_candidate(self):
        inventory = (
            entry("orphan"),                    # no route
            entry("superseded", generation=2),  # route moved on to generation 3
            entry("creating"),                  # its own create may still run
            entry("ahead", generation=5),       # newer than its route: not ours to judge
            entry("running", state="owned"),
        )
        routes = [route("superseded", 3), route("creating", 1), route("ahead", 4), route("running", 1)]

        found = stuck_start_candidates([worker("a", inventory)], routes)

        self.assertEqual([item.sandbox_id for item in found], ["orphan", "superseded"])
        self.assertEqual(found[1].generation, 2)
        self.assertEqual(found[1].operation_id, "create-superseded-2")
        self.assertEqual(found[0].node_url, "http://node-a:8090")

    def test_stale_or_partial_inventories_are_not_trusted(self):
        nodes = [
            worker("stale", (entry("x"),), fresh=False),
            worker("partial", (entry("y"),), complete=False),
        ]

        self.assertEqual(stuck_start_candidates(nodes, []), [])


class StuckStartsDueTests(unittest.TestCase):
    def test_only_candidates_stuck_past_the_minimum_age_are_due_oldest_first(self):
        now = utc_now()
        old, older, young = stuck("old"), stuck("older"), stuck("young")
        first_seen = {
            old.key: now - STUCK_START_MIN_AGE,
            older.key: now - STUCK_START_MIN_AGE - timedelta(minutes=5),
            young.key: now - STUCK_START_MIN_AGE + timedelta(seconds=1),
        }

        due = stuck_starts_due([old, young, older], first_seen, now, budget=10)

        self.assertEqual(due, [older, old])

    def test_a_candidate_seen_for_the_first_time_is_never_due(self):
        self.assertEqual(stuck_starts_due([stuck("new")], {}, utc_now(), budget=10), [])

    @given(st.integers(min_value=0, max_value=20), st.integers(min_value=-3, max_value=30))
    def test_never_more_than_the_budget(self, count, budget):
        now = utc_now()
        items = [stuck(f"s{index}") for index in range(count)]
        first_seen = {item.key: now - STUCK_START_MIN_AGE for item in items}

        self.assertEqual(len(stuck_starts_due(items, first_seen, now, budget=budget)),
                         min(count, max(0, budget)))


class StuckStartSightingTests(unittest.TestCase):
    def test_age_counts_only_an_unbroken_run_of_sightings(self):
        start = utc_now()
        with tempfile.TemporaryDirectory() as raw_dir:
            path = Path(raw_dir) / "autoscaler.sqlite"
            a, b = stuck("a").key, stuck("b").key

            AutoscalerStateStore(path).track_stuck_starts(deployment_id="d", keys=[a, b], now=start)
            later = AutoscalerStateStore(path).track_stuck_starts(
                deployment_id="d", keys=[a], now=start + timedelta(minutes=10))
            self.assertEqual(set(later), {a})
            self.assertEqual(int(later[a].timestamp()), int(start.timestamp()))

            back = AutoscalerStateStore(path).track_stuck_starts(
                deployment_id="d", keys=[a, b], now=start + timedelta(minutes=30))
            self.assertEqual(int(back[a].timestamp()), int(start.timestamp()))
            self.assertEqual(int(back[b].timestamp()), int((start + timedelta(minutes=30)).timestamp()))

    def test_deployments_track_their_own_sightings(self):
        now = utc_now()
        with tempfile.TemporaryDirectory() as raw_dir:
            store = AutoscalerStateStore(Path(raw_dir) / "autoscaler.sqlite")
            key = stuck("a").key
            store.track_stuck_starts(deployment_id="one", keys=[key], now=now - timedelta(hours=1))

            other = store.track_stuck_starts(deployment_id="two", keys=[key], now=now)

            self.assertEqual(int(other[key].timestamp()), int(now.timestamp()))


if __name__ == "__main__":
    unittest.main()
