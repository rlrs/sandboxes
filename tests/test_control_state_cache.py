from dataclasses import replace
from pathlib import Path
import json
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tests.test_registry import build_heartbeat
from sandboxes import control_state
from sandboxes.control_state import ControlStateStore
from sandboxes.models import (
    NODE_RUNTIME_METRIC_DEFAULTS, NodeRuntimeMetrics, SandboxInventoryEntry, utc_now,
)

TEST_TIER = "contract"


class ControlStateCacheTests(unittest.TestCase):
    def test_persisted_rows_may_omit_only_defaulted_metric_fields(self):
        # NODE_RUNTIME_METRIC_DEFAULTS holds a new field for one release, so
        # rows the previous gateway wrote still load after an upgrade.
        from scripts.release.verify_heartbeat_upgrade import verify

        def canonical(value):
            return json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)

        with TemporaryDirectory() as directory:
            path = Path(directory) / "control.sqlite"
            ControlStateStore(path).upsert_heartbeat(
                replace(self.heartbeat(), runtime_metrics=NodeRuntimeMetrics(collected_at=utc_now())))
            with sqlite3.connect(path) as connection:
                raw = json.loads(connection.execute("SELECT payload FROM control_records").fetchone()[0])
                raw["runtime_metrics"].pop("memory_working_set_mb")
                payload = canonical(raw)
                connection.execute("UPDATE control_records SET payload = ?", (payload,))
            with self.assertRaisesRegex(ValueError, "invalid heartbeat"):
                verify(path)
            with patch.dict(NODE_RUNTIME_METRIC_DEFAULTS, {"memory_working_set_mb": 0}):
                self.assertEqual(verify(path), 1)
                # Open a fresh store as the upgraded gateway/autoscaler does.
                reader = ControlStateStore(path)
                self.assertEqual(reader.load_heartbeats()["job"].runtime_metrics.memory_working_set_mb, 0)
                self.assertTrue(reader.get_heartbeat("job").inventory_complete)
                self.assertFalse(reader.get_heartbeat("job", include_inventory=False).inventory_complete)
                for corrupt in (" " + payload, canonical({**raw, "unexpected": True})):
                    with sqlite3.connect(path) as connection:
                        connection.execute("UPDATE control_records SET payload = ?", (corrupt,))
                    with self.assertRaisesRegex(ValueError, "invalid heartbeat"):
                        reader.load_heartbeats()

    def test_header_read_omits_inventory_but_rechecks_external_authority(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "control.sqlite"
            reader, writer = ControlStateStore(path), ControlStateStore(path)
            writer.receive_heartbeat(replace(self.heartbeat(), received_at=utc_now()))
            header = reader.get_heartbeat("job", include_inventory=False)
            self.assertEqual(header.inventory, ())
            self.assertFalse(header.inventory_complete)
            self.assertEqual(header.node_epoch, "boot-1")
            header.labels["pool"] = "changed"
            full = reader.get_heartbeat("job")
            self.assertTrue(full.inventory_complete)
            self.assertEqual(len(full.inventory), 1)
            self.assertEqual(full.labels["pool"], "workers")
            writer.quarantine_node("job", "test")
            self.assertFalse(reader.get_heartbeat("job", include_inventory=False).admission_open)
            writer.receive_heartbeat(replace(
                self.heartbeat(), node_epoch="boot-2", received_at=utc_now(),
            ))
            self.assertEqual(reader.get_heartbeat("job", include_inventory=False).node_epoch, "boot-2")
            with sqlite3.connect(path) as connection:
                connection.execute("UPDATE control_records SET payload = ' ' || payload")
            with self.assertRaisesRegex(ValueError, "invalid heartbeat"):
                reader.get_heartbeat("job", include_inventory=False)
            with sqlite3.connect(path) as connection:
                connection.execute("DELETE FROM control_records")
            self.assertIsNone(reader.get_heartbeat("job", include_inventory=False))

    def heartbeat(self, job_id="job"):
        return build_heartbeat(
            job_id=job_id, node_id=f"node-{job_id}",
            node_url=f"http://node-{job_id}:8090", node_epoch="boot-1",
            labels={"pool": "workers"}, inventory_complete=True,
            inventory=(SandboxInventoryEntry(
                sandbox_id="sandbox", generation=1, operation_id="create-1",
                spec_hash="a" * 64, state="running",
                storage_dependency={"layers": [{"digest": "original"}]},
            ),),
        )

    def test_unchanged_headers_do_not_reload_inventory_and_isolate_labels(self):
        with TemporaryDirectory() as directory:
            store = ControlStateStore(Path(directory) / "control.sqlite")
            store.upsert_heartbeat(self.heartbeat())
            store.get_heartbeat("job", include_inventory=False)
            statements = []
            with store._connection() as connection:
                connection.set_trace_callback(statements.append)
            first = store.get_heartbeat("job", include_inventory=False)
            first.labels["pool"] = "changed"
            second = store.get_heartbeat("job", include_inventory=False)
            self.assertEqual(second.labels["pool"], "workers")
            self.assertEqual(second.inventory, ())
            self.assertFalse(second.inventory_complete)
            self.assertFalse(any("SELECT payload" in sql for sql in statements))

    def test_header_cache_observes_own_connection_writes_and_rollback(self):
        with TemporaryDirectory() as directory:
            store = ControlStateStore(Path(directory) / "control.sqlite")
            store.upsert_heartbeat(self.heartbeat())
            self.assertEqual(store.get_heartbeat("job", include_inventory=False).node_epoch, "boot-1")
            with store._connection() as connection:
                version = connection.execute("PRAGMA data_version").fetchone()[0]
            store.upsert_heartbeat(replace(self.heartbeat(), node_epoch="boot-2"))
            with store._connection() as connection:
                self.assertEqual(connection.execute("PRAGMA data_version").fetchone()[0], version)
            self.assertEqual(store.get_heartbeat("job", include_inventory=False).node_epoch, "boot-2")
            with self.assertRaisesRegex(RuntimeError, "rollback"):
                with store._transaction(write=True) as connection:
                    connection.execute("DELETE FROM control_records")
                    raise RuntimeError("rollback")
            self.assertEqual(store.get_heartbeat("job", include_inventory=False).node_epoch, "boot-2")
            with store._transaction(write=True) as connection:
                connection.execute("DELETE FROM control_records")
            self.assertIsNone(store.get_heartbeat("job", include_inventory=False))
            store.upsert_heartbeat(self.heartbeat())
            self.assertEqual(store.get_heartbeat("job", include_inventory=False).node_epoch, "boot-1")

    def test_header_cache_does_not_lose_commit_racing_with_payload_load(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "control.sqlite"
            reader, writer = ControlStateStore(path), ControlStateStore(path)
            writer.upsert_heartbeat(self.heartbeat())
            original = reader._read_heartbeat

            def race(*args, **kwargs):
                writer.upsert_heartbeat(replace(self.heartbeat(), node_epoch="boot-2"))
                return original(*args, **kwargs)

            with patch.object(reader, "_read_heartbeat", side_effect=race):
                self.assertEqual(reader.get_heartbeat("job", include_inventory=False).node_epoch, "boot-1")
            self.assertEqual(reader.get_heartbeat("job", include_inventory=False).node_epoch, "boot-2")

    def test_transactional_header_does_not_cache_rolled_back_write(self):
        with TemporaryDirectory() as directory:
            store = ControlStateStore(Path(directory) / "control.sqlite")
            store.upsert_heartbeat(self.heartbeat())
            store.get_heartbeat("job", include_inventory=False)
            with store._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                _heartbeat, payload = control_state._encode_heartbeat(
                    replace(self.heartbeat(), node_epoch="uncommitted"),
                )
                connection.execute("UPDATE control_records SET payload=?", (payload,))
                self.assertEqual(store._heartbeat_header(connection, "job").node_epoch, "uncommitted")
                connection.rollback()
            self.assertEqual(store.get_heartbeat("job", include_inventory=False).node_epoch, "boot-1")

    def test_transactional_header_preserves_snapshot_then_observes_new_commit(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "control.sqlite"
            reader, writer = ControlStateStore(path), ControlStateStore(path)
            writer.upsert_heartbeat(self.heartbeat())
            reader.get_heartbeat("job", include_inventory=False)
            with reader._connection() as connection:
                connection.execute("BEGIN")
                connection.execute("SELECT payload FROM control_records").fetchall()
                writer.upsert_heartbeat(replace(self.heartbeat(), node_epoch="boot-2"))
                self.assertEqual(reader._heartbeat_header(connection, "job").node_epoch, "boot-1")
                connection.commit()
            self.assertEqual(reader.get_heartbeat("job", include_inventory=False).node_epoch, "boot-2")

    def test_header_cache_versions_remain_connection_local_and_bounded(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "control.sqlite"
            reader, writer = ControlStateStore(path), ControlStateStore(path)
            writer.upsert_heartbeat(self.heartbeat())
            with reader._connection() as first, reader._connection() as second:
                self.assertEqual(reader._heartbeat_header(first, "job").node_epoch, "boot-1")
                self.assertEqual(reader._heartbeat_header(second, "job").node_epoch, "boot-1")
                writer.upsert_heartbeat(replace(self.heartbeat(), node_epoch="boot-2"))
                self.assertEqual(reader._heartbeat_header(second, "job").node_epoch, "boot-2")
                self.assertEqual(reader._heartbeat_header(first, "job").node_epoch, "boot-2")
                with patch.object(control_state, "_HEARTBEAT_CACHE_ENTRIES", 2):
                    for index in range(4):
                        self.assertIsNone(reader._heartbeat_header(first, f"absent-{index}"))
                    self.assertLessEqual(len(first.heartbeat_headers[1]), 2)
                first.heartbeat_headers = None
                with patch.object(control_state, "_HEARTBEAT_HEADER_CACHE_BYTES", 1):
                    self.assertEqual(reader._heartbeat_header(first, "job").node_epoch, "boot-2")
                    self.assertNotIn("job", first.heartbeat_headers[1])

    def test_shared_fleet_read_reuses_cached_objects_until_payload_changes(self):
        with TemporaryDirectory() as directory:
            store = ControlStateStore(Path(directory) / "control.sqlite")
            store.upsert_heartbeat(self.heartbeat())
            first = store.load_heartbeats(shared=True)["job"]
            self.assertIs(store.load_heartbeats(shared=True)["job"], first)
            # The default read still isolates callers from the cached object.
            copied = store.load_heartbeats()["job"]
            self.assertIsNot(copied, first)
            self.assertEqual(copied, first)
            self.assertIsNot(
                copied.inventory[0].storage_dependency,
                first.inventory[0].storage_dependency,
            )
            detached = control_state.detached_heartbeat(first)
            detached.labels["pool"] = "changed"
            self.assertEqual(first.labels["pool"], "workers")
            store.upsert_heartbeat(replace(self.heartbeat(), node_epoch="boot-2"))
            refreshed = store.load_heartbeats(shared=True)["job"]
            self.assertIsNot(refreshed, first)
            self.assertEqual(refreshed.node_epoch, "boot-2")

    def test_reuses_decode_but_returns_isolated_mutable_inventory(self):
        with TemporaryDirectory() as directory:
            store = ControlStateStore(Path(directory) / "control.sqlite")
            store.upsert_heartbeat(self.heartbeat())
            with patch.object(store, "_decode_heartbeat", wraps=store._decode_heartbeat) as decode:
                first = store.get_heartbeat("job")
                first.labels["pool"] = "changed"
                first.inventory[0].storage_dependency["layers"][0]["digest"] = "changed"
                first.inventory[0].storage_snapshot["unexpected"] = True
                second = store.load_heartbeats()["job"]
                third = store.get_heartbeat("job")
                self.assertEqual(decode.call_count, 1)
            for heartbeat in (second, third):
                self.assertEqual(heartbeat.labels, {"pool": "workers"})
                self.assertEqual(heartbeat.inventory[0].storage_snapshot, {})
                self.assertEqual(heartbeat.inventory[0].storage_dependency,
                                 {"layers": [{"digest": "original"}]})

    def test_other_connection_quarantine_reboot_delete_and_corruption_are_immediate(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "control.sqlite"
            reader, writer = ControlStateStore(path), ControlStateStore(path)
            heartbeat = replace(self.heartbeat(), received_at=utc_now())
            writer.receive_heartbeat(heartbeat)
            self.assertTrue(reader.get_heartbeat("job").admission_open)
            writer.quarantine_node("job", "test")
            self.assertFalse(reader.get_heartbeat("job").admission_open)
            writer.receive_heartbeat(replace(
                heartbeat, node_epoch="boot-2", received_at=utc_now(),
            ))
            current = reader.get_heartbeat("job")
            self.assertEqual(current.node_epoch, "boot-2")
            self.assertEqual(current.retired_node_epochs, ("boot-1",))
            self.assertFalse(current.admission_open)
            with sqlite3.connect(path) as connection:
                connection.execute("UPDATE control_records SET payload = ' ' || payload")
            for load in (lambda: reader.get_heartbeat("job"), reader.load_heartbeats):
                with self.assertRaisesRegex(ValueError, "invalid heartbeat"):
                    load()
            with sqlite3.connect(path) as connection:
                connection.execute("DELETE FROM control_records")
            self.assertIsNone(reader.get_heartbeat("job"))
            self.assertEqual(reader.load_heartbeats(), {})

    def test_eviction_and_oversized_rows_do_not_limit_fleet_reads(self):
        with TemporaryDirectory() as directory:
            store = ControlStateStore(Path(directory) / "control.sqlite")
            with patch.object(control_state, "_HEARTBEAT_CACHE_ENTRIES", 2):
                for index in range(4):
                    store.upsert_heartbeat(self.heartbeat(str(index)))
                self.assertEqual(len(store.load_heartbeats()), 4)
                self.assertLessEqual(len(store._heartbeat_cache), 2)
            # Force byte eviction separately from entry eviction.
            with patch.object(control_state, "_HEARTBEAT_CACHE_BYTES", 1):
                store.upsert_heartbeat(self.heartbeat("large"))
                self.assertEqual(store.get_heartbeat("large").node_id, "node-large")
                self.assertNotIn("large", store._heartbeat_cache)
            self.assertEqual(len(store.load_heartbeats()), 5)

    def test_file_identity_check_does_not_block_returning_another_reader(self):
        from threading import Event, Thread, current_thread
        with TemporaryDirectory() as raw:
            store = ControlStateStore(Path(raw) / 'control.sqlite')
            entered, release, returned = Event(), Event(), Event()
            original = Path.stat
            errors = []
            def slow_stat(path, *args, **kwargs):
                if path == store.path and current_thread().name == 'slow-reader':
                    entered.set()
                    release.wait(2)
                return original(path, *args, **kwargs)
            held = store._connection()
            held.__enter__()
            store._connection_checked_at = float('-inf')  # force the reader's recheck
            def read():
                try:
                    store.load_heartbeats()
                except BaseException as exc:
                    errors.append(exc)
            def give_back():
                try:
                    held.__exit__(None, None, None)
                finally:
                    returned.set()
            with patch.object(Path, 'stat', slow_stat):
                reader = Thread(target=read, name='slow-reader')
                reader.start()
                self.assertTrue(entered.wait(2))
                returning = Thread(target=give_back)
                returning.start()
                try:
                    self.assertTrue(returned.wait(.5), 'identity stat held the pool mutex')
                finally:
                    release.set()
                    reader.join(3)
                    returning.join(3)
            self.assertFalse(errors)

    def test_exec_absence_check_only_loads_inventory_for_an_empty_worker(self):
        from sandboxes.exec_routing import ExecRoutingService, heartbeat_proves_route_absent
        from sandboxes.routing import ExecRoute
        with TemporaryDirectory() as raw:
            store = ControlStateStore(Path(raw) / 'control.sqlite')
            heartbeat = replace(self.heartbeat(), received_at=utc_now(), active_sandboxes=1)
            store.receive_heartbeat(heartbeat)
            routing = ExecRoutingService(store, None, 120)
            route = ExecRoute(session_id='exec', sandbox_id='sandbox', node_id=heartbeat.node_id,
                              job_id=heartbeat.job_id, node_url=heartbeat.node_url,
                              created_at='2026-01-01T00:00:00+00:00', updated_at='2026-01-01T00:00:00+00:00')
            def stale():
                return heartbeat_proves_route_absent(
                    routing.heartbeat(route), sandbox_id=route.sandbox_id,
                    route_created_at=route.created_at, route_updated_at=route.updated_at,
                    heartbeat_ttl_seconds=routing.heartbeat_ttl_seconds,
                )
            with patch.object(store, 'get_heartbeat', wraps=store.get_heartbeat) as get:
                self.assertFalse(stale())
                get.assert_called_once_with('job', include_inventory=False)
            # Zero active count does not mean the parked sandbox disappeared.
            store.receive_heartbeat(replace(heartbeat, active_sandboxes=0, received_at=utc_now()))
            self.assertFalse(stale())
            store.receive_heartbeat(replace(heartbeat, active_sandboxes=0, inventory=(), received_at=utc_now()))
            self.assertTrue(stale())

    def test_single_heartbeat_read_has_no_explicit_transaction_and_keeps_permissions(self):
        with TemporaryDirectory() as directory:
            store = ControlStateStore(Path(directory) / 'control.sqlite')
            store.upsert_heartbeat(self.heartbeat())
            statements = []
            with store._connection() as conn:
                conn.set_trace_callback(statements.append)
            store.path.chmod(0o644)
            store._connection_checked_at = float('-inf')  # file checks run once a second
            self.assertEqual(store.get_heartbeat('job').job_id, 'job')
            self.assertFalse(any(s.startswith(('BEGIN', 'COMMIT')) for s in statements))
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)
            for suffix in ('-wal', '-shm'):
                path = Path(str(store.path) + suffix)
                if path.exists():
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_body_pooling_requires_a_fresh_heartbeat_for_the_origin(self):
        from datetime import timedelta
        from tests.gateway_support import gateway_services
        with TemporaryDirectory() as directory:
            fleet = gateway_services(store=ControlStateStore(Path(directory) / 'control.sqlite'),
                                     heartbeat_ttl_seconds=30).fleet
            for age, expected in [(0, 'http://node-job:8090'), (60, None)]:
                heartbeat = replace(self.heartbeat(), received_at=utc_now() - timedelta(seconds=age))
                self.assertEqual(fleet.body_keepalive_origin(heartbeat), expected)
            self.assertIsNone(fleet.body_keepalive_origin(None))
