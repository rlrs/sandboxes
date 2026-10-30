"""Operator drain of a worker (POST/DELETE /v1/nodes/<job>/drain).

Tier: contract. A drained worker takes no new work at once, is no capacity to
plan with, and stops through the drain handshake once idle; evict deletes what
it holds. The autoscaler's handshake itself is covered by tests/test_cli.py.
"""

from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sandboxes.control_state import ControlStateStore
from sandboxes.models import (
    OPERATOR_DRAIN_LABEL,
    InstancePhase,
    NodeHeartbeat,
    ProviderInstance,
    ResourceQuantity,
    SandboxDemand,
    SandboxNode,
    SandboxPlacementRequest,
    ScalePolicy,
    retiring_reason,
    utc_now,
)
from sandboxes.policy import evaluate_scale
from tests.harness import LocalFleet

TEST_TIER = "contract"
CAPACITY = ResourceQuantity(vcpu=32, memory_mb=98_304, disk_mb=1_000_000)


def worker(job_id: str, *, active: int, drained: bool = False, idle_for: timedelta | None = None) -> SandboxNode:
    now = utc_now()
    heartbeat = NodeHeartbeat(
        node_id=f"node-{job_id}", job_id=job_id, updated_at=now - timedelta(seconds=5),
        active_sandboxes=active, total_resources=CAPACITY, resources_known=True,
        idle_since=now - idle_for if idle_for else None,
        labels={OPERATOR_DRAIN_LABEL: now.isoformat()} if drained else {},
    )
    job = ProviderInstance(
        id=job_id, name=f"node-{job_id}", application_name="vm", application_version="1",
        product_id="p", product_category="c", state="RUNNING", phase=InstancePhase.RUNNING,
        created_at=now - timedelta(hours=1), started_at=now - timedelta(hours=1), cpu=32, memory_gb=96,
    )
    return SandboxNode(job=job, heartbeat=heartbeat, active_sandboxes=active, heartbeat_fresh=True)


class OperatorDrainDecisionTests(unittest.TestCase):
    def test_drained_worker_is_no_capacity_and_stops_once_idle(self):
        now = utc_now()
        busy = worker("200", active=5, drained=True)
        self.assertEqual(retiring_reason(busy.heartbeat, now), "drained by an operator")

        request = SandboxPlacementRequest(ResourceQuantity(1, 1024, 2048))
        waiting = SandboxDemand(pending_count=1, pending_resources=request.resources, placement_requests=(request,))
        decision = evaluate_scale([busy], waiting, ScalePolicy(max_nodes=4), now=now)
        self.assertEqual(decision.ready_nodes, 0)  # Demand is planned without it: a replacement.
        self.assertEqual(decision.stops, ())  # It still holds sandboxes.

        idle = worker("200", active=0, drained=True, idle_for=timedelta(seconds=1))
        decision = evaluate_scale([worker("100", active=3), idle], SandboxDemand(), ScalePolicy(), now=now)
        self.assertEqual(decision.stops, ("200",))  # No idle grace for an operator drain.


class OperatorDrainControlStateTests(unittest.TestCase):
    def heartbeat(self, **values) -> NodeHeartbeat:
        fields = {"node_id": "node-300", "job_id": "300", "deployment_id": "prod", "updated_at": utc_now(),
                  "received_at": utc_now(), "active_sandboxes": 2, "node_epoch": "epoch-1", "activity_epoch": 1,
                  "inventory_complete": True}
        return NodeHeartbeat(**{**fields, **values})

    def test_label_closes_placement_admission_and_outlives_receipts(self):
        with TemporaryDirectory() as raw:
            store = ControlStateStore(Path(raw) / "control.sqlite")
            store.upsert_heartbeat(self.heartbeat())
            self.assertFalse(store.set_operator_drain("missing", "t0"))
            self.assertTrue(store.set_operator_drain("300", "t0"))

            store.receive_heartbeat(self.heartbeat(activity_epoch=2, labels={OPERATOR_DRAIN_LABEL: "forged"}))
            self.assertEqual(store.get_heartbeat("300").labels[OPERATOR_DRAIN_LABEL], "t0")
            self.assertFalse(store.load_heartbeats()["300"].admission_open)

            self.assertTrue(store.clear_operator_drain("300"))
            store.receive_heartbeat(self.heartbeat(activity_epoch=3))
            self.assertNotIn(OPERATOR_DRAIN_LABEL, store.get_heartbeat("300").labels)
            self.assertTrue(store.load_heartbeats()["300"].admission_open)


class OperatorDrainRouteTests(unittest.TestCase):
    def test_drain_moves_creates_elsewhere_evicts_and_reopens(self):
        with LocalFleet(nodes=2) as fleet:
            fleet.create("alpha")
            drained = fleet.node_for("alpha")
            other = next(node for node in fleet.nodes if node is not drained)
            path = f"/v1/nodes/{drained.job_id}/drain"

            self.assertEqual(fleet.request("POST", path, payload={}, token="sandbox").status, 403)
            self.assertEqual(fleet.request("POST", "/v1/nodes/no-such-job/drain", payload={}).status, 404)
            self.assertEqual(fleet.request("POST", path, payload={"evict": "yes"}).status, 400)
            response = fleet.request("POST", path, payload={})
            self.assertEqual((response.status, response.json()), (200, {"job_id": drained.job_id, "draining": True}))

            for index in range(3):
                fleet.create(f"beta-{index}")
                self.assertIs(fleet.node_for(f"beta-{index}"), other)
            self.assertIs(fleet.node_for("alpha"), drained)  # What it holds keeps running.

            evicted = fleet.request("POST", path, payload={"evict": True})
            self.assertEqual(evicted.status, 200, evicted.body)
            self.assertEqual([item["id"] for item in evicted.json()["evicted"]], ["alpha"])
            self.assertIsNone(fleet.route("alpha"))

            self.assertEqual(fleet.request("DELETE", path).json(), {"job_id": drained.job_id, "draining": False})
            created_on = {fleet.node_for(sandbox).job_id for sandbox in
                          (fleet.create(f"gamma-{index}")["id"] for index in range(6))}
            self.assertIn(drained.job_id, created_on)


if __name__ == "__main__":
    unittest.main()
