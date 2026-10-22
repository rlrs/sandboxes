from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
from threading import Event
import unittest
from unittest.mock import patch

from tests.support import wait_until
from tests.test_control_plane import _gateway_server, _sandbox_route, build_heartbeat
from tests import test_wake_capacity as capacity_fixtures
from sandboxes import control_plane
from sandboxes.models import ResourceQuantity

TEST_TIER = "contract"


class WakeBatchTests(unittest.TestCase):
    def fixture(self, root, count, *, device_bound=False):
        server = _gateway_server(self, root)
        self.addCleanup(server.server_close)
        handler = object.__new__(server.RequestHandlerClass)
        owner = (capacity_fixtures.WakeCapacityTests().heartbeat(active=63) if device_bound else build_heartbeat(
            node_id="node", job_id="job", node_url="http://node:8090",
            capabilities=("sandbox", "disk-quota"),
            total_resources=ResourceQuantity(vcpu=32, memory_mb=98304, disk_mb=1000000),
        ))
        handler.services.fleet.store.upsert_heartbeat(owner)
        routes = []
        for n in range(count):
            route = _sandbox_route(
                sandbox_id=f"local-{n}", node_id="node", job_id="job",
                node_url="http://node:8090", state="parked",
                resources=ResourceQuantity(vcpu=1, memory_mb=1024, disk_mb=2048),
                spec={"parkable": True, "managed_process": True},
            )
            routes.append(handler.routing_store.upsert_sandbox(route))
        return handler, routes

    def test_batch_respects_device_capacity_and_deduplicates_wake(self):
        with TemporaryDirectory() as temp:
            handler, routes = self.fixture(Path(temp), 3, device_bound=True)
            results = handler.services.placement.atomic(lambda: handler._reserve_local_wake_batch([
                (handler, r, None) for r in [routes[0], routes[0], *routes[1:]]
            ]), worker_id="job")
            self.assertEqual([r.state if r else None for r in results],
                             ["waking", "waking", None, None])
            self.assertEqual([handler.routing_store.get_sandbox(r.sandbox_id).state
                              for r in routes], ["waking", "parked", "parked"])

    def test_waiting_callers_share_commit_and_observe_durable_results(self):
        with TemporaryDirectory() as temp:
            handler, routes = self.fixture(Path(temp), 12)
            batcher = control_plane._LocalWakeBatcher()
            held, release = Event(), Event()

            def hold_worker_turn():
                held.set()
                release.wait(10)

            def drained_first():
                with batcher.lock:
                    return batcher.running and not batcher.pending

            def queued_rest():
                with batcher.lock:
                    return len(batcher.pending) == len(routes) - 1

            with patch.object(handler.routing_store, "reserve_sandbox_wakes",
                              wraps=handler.routing_store.reserve_sandbox_wakes) as commit:
                with ThreadPoolExecutor(max_workers=len(routes) + 1) as pool:
                    # Another placement on this worker holds its turn, so the
                    # first wake's batch waits and the rest queue behind it.
                    holder = pool.submit(handler.services.placement.atomic, hold_worker_turn,
                                         worker_id="job")
                    self.assertTrue(held.wait(5))
                    futures = [pool.submit(batcher.reserve, handler, routes[0])]
                    wait_until(drained_first)
                    futures += [pool.submit(batcher.reserve, handler, r) for r in routes[1:]]
                    wait_until(queued_rest)
                    self.assertFalse(any(f.done() for f in futures))
                    release.set()
                    holder.result(timeout=5)
                    for f in futures:
                        result = f.result(timeout=5)
                        self.assertEqual(handler.routing_store.get_sandbox(result.sandbox_id), result)
                self.assertEqual([len(call.args[0]) for call in commit.call_args_list],
                                 [1, len(routes) - 1])

    def test_failed_commit_rolls_back_every_reservation_and_unblocks_callers(self):
        with TemporaryDirectory() as temp:
            handler, routes = self.fixture(Path(temp), 2)
            store = handler.routing_store
            with store._transaction() as conn:
                conn.execute("""CREATE FUNCTION reject_second() RETURNS trigger LANGUAGE plpgsql AS $$
                    BEGIN
                        IF NEW.sandbox_id = 'local-1' AND NEW.state = 'waking' THEN
                            RAISE EXCEPTION 'injected failure';
                        END IF;
                        RETURN NEW;
                    END $$""")
                conn.execute("""CREATE TRIGGER reject_second BEFORE UPDATE ON sandboxes
                    FOR EACH ROW EXECUTE FUNCTION reject_second()""")
            with self.assertRaises(sqlite3.DatabaseError):
                store.reserve_sandbox_wakes([(r, 'wake:' + r.sandbox_id) for r in routes])
            self.assertEqual([store.get_sandbox(r.sandbox_id).state for r in routes],
                             ["parked", "parked"])
            batcher = control_plane._LocalWakeBatcher()
            with self.assertRaises(sqlite3.DatabaseError):
                batcher.reserve(handler, routes[1])
            with store._transaction() as conn:
                conn.execute("DROP TRIGGER reject_second ON sandboxes")
            self.assertEqual(batcher.reserve(handler, routes[1]).state, "waking")

    def test_batch_rechecks_owner_and_keeps_stale_pending_demand(self):
        with TemporaryDirectory() as temp:
            handler, routes = self.fixture(Path(temp), 2)
            store = handler.routing_store
            stale = replace(routes[1], node_id="different")
            for r in routes:
                store.upsert_pending_with_demand("wake:" + r.sandbox_id, r.resources)
            results = store.reserve_sandbox_wakes([
                (routes[0], "wake:" + routes[0].sandbox_id),
                (stale, "wake:" + stale.sandbox_id),
            ])
            self.assertEqual(results[routes[0].sandbox_id].state, "waking")
            self.assertIsNone(results[stale.sandbox_id])
            with store._connect() as conn:
                pending = {r[0] for r in conn.execute("SELECT sandbox_id FROM pending")}
            self.assertEqual(pending, {"wake:" + stale.sandbox_id})
