"""A delete that lands while the worker is still resolving a create's image.

Tier: contract. The worker registers a sandbox only once its image resolves,
so a DELETE in that window finds nothing to delete and the gateway drops the
route. The create then finishes on the worker; the gateway must delete that
incarnation instead of leaving it running with no route (2026-11-04).
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import unittest

from sandboxes.models import ResourceQuantity
from sandboxes.routing import SandboxRoute
from sandboxes.worker_receipts import create_incarnation_unowned
from tests.harness import LocalFleet
from tests.support import wait_until

TEST_TIER = "contract"
SPEC = {"cpus": 1, "memory_mb": 256, "disk_mb": 1024, "network": "none"}


def route(generation: int, **changes) -> SandboxRoute:
    return replace(SandboxRoute(
        sandbox_id="s", node_id="node-a", job_id="a", node_url="http://node-a:8090",
        resources=ResourceQuantity(), spec={"id": "s"}, state="creating", generation=generation,
        create_operation_id=f"create-{generation}", spec_hash="0" * 64,
    ), **changes)


class UnownedCreateTests(unittest.TestCase):
    def test_a_create_loses_its_incarnation_only_to_a_delete_or_a_newer_generation(self):
        created = route(2)
        self.assertTrue(create_incarnation_unowned(None, created))
        self.assertTrue(create_incarnation_unowned(route(2, delete_operation_id="delete-1"), created))
        self.assertTrue(create_incarnation_unowned(route(3), created))
        self.assertFalse(create_incarnation_unowned(route(2, state="running"), created))
        self.assertFalse(create_incarnation_unowned(route(1), created))


class CreateDeleteRaceTests(unittest.TestCase):
    def race(self, fleet, sandbox_id, create, delete):
        node = fleet.nodes[0]
        hold = node.rootfs_store.hold("operation_lease")
        with ThreadPoolExecutor(1) as pool:
            created = pool.submit(create)
            self.assertTrue(hold.reached.wait(10), "the create never reached the worker")
            deleted = delete()
            self.assertEqual(deleted.status, 200, deleted.body)
            self.assertIsNone(fleet.route(sandbox_id))
            hold.release.set()
            created.result(timeout=30)
        wait_until(lambda: node.registration(sandbox_id) is None,
                   message=f"{sandbox_id} is still on the worker with no route")

    def test_a_group_deleted_mid_create_leaves_nothing_on_the_worker(self):
        with LocalFleet(nodes=1, environments=False) as fleet:
            self.race(
                fleet, "late-0000",
                lambda: fleet.request("POST", "/v1/sandboxes:batch", token="sandbox", payload={
                    "group_id": "late", "count": 1, "spec": {**SPEC, "image": fleet.image}}),
                lambda: fleet.request("DELETE", "/v1/sandboxes:batch/late", token="sandbox"),
            )

    def test_a_sandbox_deleted_mid_create_leaves_nothing_on_the_worker(self):
        with LocalFleet(nodes=1, environments=False) as fleet:
            self.race(
                fleet, "late",
                lambda: fleet.request("POST", "/v1/sandboxes", token="sandbox",
                                      payload={"id": "late", "image": fleet.image, **SPEC}),
                lambda: fleet.delete("late"),
            )


if __name__ == "__main__":
    unittest.main()
