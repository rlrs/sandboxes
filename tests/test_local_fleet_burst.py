"""S8: create burst admission and queue, over the local fleet harness.

Tier: contract. Concurrent creates across three workers never overbook a
worker's disk, the binding resource here (8 GiB sandboxes on 64 GiB
workers). A worker that closed admission without the gateway knowing makes
its creates reselect elsewhere under a new generation, or, with no other
worker, refuse at the image pull or the create with the demand still
queued. Which creates a burst refuses depends on timing, but every refusal
is retryable JSON with its demand queued, so a client that retries fills
the open workers exactly; once nothing fits, the refusal carries the
pending demand. The gateway's own create cap answers a retryable busy.

Not covered: the PostgreSQL durable placement queue (client disconnect,
coalesced duplicates), which needs ``queue_placement``.
"""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest

from tests.harness import LocalFleet
from sandboxes import routing

SPEC = {"cpus": 1, "memory_mb": 256, "disk_mb": 8192, "network": "none"}
PER_NODE = 65536 // SPEC["disk_mb"]


class CreateBurstTests(unittest.TestCase):
    def test_burst_reselects_around_a_closed_worker_without_overbooking(self):
        with LocalFleet(nodes=3) as fleet:
            closed, *open_nodes = fleet.nodes
            # Closed at the worker only; the gateway still samples it.
            self.assertEqual(closed.drain("burst").status, 200)

            def create(name):
                return fleet.request("POST", "/v1/sandboxes", payload={"id": name, **SPEC, "image": fleet.image}, token="sandbox")

            names = [f"burst-{index}" for index in range(2 * PER_NODE + 4)]
            with ThreadPoolExecutor(len(names)) as pool:
                responses = dict(zip(names, pool.map(create, names)))

            # A worker whose startup slots stay busy past a candidate's short
            # admission wait defers it, so the burst may refuse a create while
            # an open worker has room. Every refusal is a definite reject with
            # its demand queued, never pinned to a worker.
            burst = [name for name in names if responses[name].status == 201]
            refused = [name for name in names if name not in burst]
            pending = routing.open_routing_store(fleet.routing_file).load().pending
            for name in refused:
                response = responses[name]
                self.assertEqual((response.status, response.json()["retryable"]), (503, True), response.body)
                error_code = response.json()["error_code"]
                self.assertIn(error_code, {"node_admission_closed", "node_active_admission_deferred"})
                self.assertIsNone(fleet.route(name))
                self.assertEqual(pending[name].failure_reason, error_code)
            placed = Counter(fleet.route(name).job_id for name in burst)
            self.assertLessEqual(set(placed), {node.job_id for node in open_nodes})
            self.assertLessEqual(max(placed.values(), default=0), PER_NODE)
            # Creates first placed on the closed worker came back reselected.
            self.assertTrue(any(fleet.route(name).generation == 2 for name in burst))

            # Once the gateway knows, the client's retries fill the open
            # workers exactly; the rest find nothing that fits.
            fleet.heartbeat()
            retries = {name: create(name) for name in refused}
            created = burst + [name for name in refused if retries[name].status == 201]
            overflow = [name for name in refused if name not in created]
            pending = routing.open_routing_store(fleet.routing_file).load().pending
            # A placed create consumed the demand its refusal queued.
            self.assertFalse(set(created) & set(pending))
            self.assertEqual(Counter(fleet.route(name).job_id for name in created),
                             {node.job_id: PER_NODE for node in open_nodes})
            # The closed worker refused at its image pull, before any create.
            self.assertIn(("POST", "/v1/images/pull"), closed.requests)
            self.assertNotIn(("POST", "/v1/sandboxes"), closed.requests)
            self.assertTrue(all(closed.registration(name) is None for name in names))
            self.assertEqual(closed.live_sentries(), [])
            for node in open_nodes:
                reported = node.post_heartbeat()["node"]
                self.assertEqual((reported["active_sandboxes"], reported["used_resources"]["disk_mb"]),
                                 (PER_NODE, 65536))

            # The refusal carries demand, and the demand view lists it until
            # the client gives up.
            for name in overflow:
                refused_again = retries[name]
                self.assertEqual(refused_again.status, 503, refused_again.body)
                self.assertEqual((refused_again.json()["error_code"], refused_again.json()["retryable"]),
                                 ("no_ready_node", True))
                self.assertEqual(refused_again.headers["X-UCloud-Sandbox-Retryable"], "true")
                self.assertTrue(refused_again.headers["Retry-After"].isdigit())
                self.assertGreaterEqual(refused_again.json()["pending_resources"]["disk_mb"], SPEC["disk_mb"])
            demand = fleet.request("GET", "/v1/demand").json()
            self.assertLessEqual(set(overflow), {item["sandbox_id"] for item in demand["pending"]})
            self.assertEqual(fleet.delete(overflow[0]).status, 200)
            demand = fleet.request("GET", "/v1/demand").json()
            self.assertNotIn(overflow[0], [item["sandbox_id"] for item in demand["pending"]])

    def test_closed_worker_without_alternate_keeps_retryable_demand(self):
        with LocalFleet() as fleet:
            node = fleet.nodes[0]
            # The worker refuses at the image pull, or at the create once the
            # gateway knows the image is cached. Either answer is forwarded and
            # the demand stays queued, never pinned to that worker.
            for name, refused_at in (("cold", "/v1/images/pull"), ("warm", "/v1/sandboxes")):
                if name == "warm":
                    self.assertEqual(node.drain("solo", draining=False).status, 200)
                    fleet.create("seed")
                    fleet.heartbeat()
                self.assertEqual(node.drain("solo").status, 200)
                before = len(node.requests)
                refused = fleet.request("POST", "/v1/sandboxes", payload={"id": name, **SPEC, "image": fleet.image}, token="sandbox")
                self.assertEqual(refused.status, 503, refused.body)
                self.assertEqual((refused.json()["error_code"], refused.json()["retryable"]),
                                 ("node_admission_closed", True))
                self.assertEqual([path for method, path in node.requests[before:] if method == "POST"], [refused_at])
                self.assertIsNone(fleet.route(name))
                pending = routing.open_routing_store(fleet.routing_file).load().pending
                self.assertEqual(pending[name].failure_reason, "node_admission_closed")

    def test_gateway_create_cap_answers_retryable_busy(self):
        with LocalFleet(max_concurrent_sandbox_creates=1, admission_wait_seconds=0.2) as fleet:
            node = fleet.nodes[0]
            node.arm_fault("create", "hang", sandbox_id="first")
            result: dict = {}
            first = threading.Thread(target=lambda: result.setdefault("response", fleet.request(
                "POST", "/v1/sandboxes", payload={"id": "first", **SPEC, "image": fleet.image}, token="sandbox")), daemon=True)
            first.start()
            node.wait_hung("create")

            busy = fleet.request("POST", "/v1/sandboxes", payload={"id": "second", **SPEC, "image": fleet.image}, token="sandbox")
            self.assertEqual((busy.status, busy.json()), (503, {
                "error": "gateway admission wait deadline exceeded",
                "error_code": "gateway_startup_busy",
                "retryable": True,
                "max_concurrent_sandbox_creates": 1,
            }))
            self.assertEqual((busy.headers["Retry-After"], busy.headers["X-UCloud-Sandbox-Retryable"]), ("1", "true"))
            self.assertIsNone(fleet.route("second"))

            node.release("create")
            first.join(15)
            self.assertEqual(result["response"].status, 201, result["response"].body)
            self.assertEqual(fleet.create("second")["state"], "running")


if __name__ == "__main__":
    unittest.main()
