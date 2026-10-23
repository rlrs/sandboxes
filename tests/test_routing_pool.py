from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from tests.test_control_plane import _sandbox_route
from tests.support import routing_store_for
from sandboxes.routing import RoutingUnavailable

TEST_TIER = "contract"


class RoutingPoolTests(unittest.TestCase):
    def test_bulk_fleet_snapshot_preserves_values_order_and_fails_closed(self):
        with TemporaryDirectory() as tmp:
            store = routing_store_for(self, Path(tmp) / 'routing.sqlite')
            self.assertEqual(store.sandbox_routes_readonly(), [])
            routes = [store.upsert_sandbox(_sandbox_route(
                sandbox_id=sid, state='running', node_id='node', job_id='job',
                node_url='http://node', node_epoch='boot',
                spec={'id': sid, 'labels': {'quoted': '"\\\n\u0000æ'}, 'nested': [True, None, 3.5]},
                activity_epoch=2**54,
            )) for sid in ['z', 'a']]
            snapshot = store.sandbox_routes_readonly()
            self.assertEqual(snapshot, sorted(routes, key=lambda r: r.sandbox_id))
            snapshot[0].spec['nested'].append('caller mutation')
            self.assertEqual(store.sandbox_routes_readonly()[0].spec['nested'], [True, None, 3.5])
            with store._transaction() as conn:
                conn.execute("UPDATE sandboxes SET spec_json='[]' WHERE sandbox_id='a'")
            with self.assertRaises(RoutingUnavailable):
                store.sandbox_routes_readonly()

    def test_lifecycle_update_preserves_spec_generation_and_storage_dependency(self):
        with TemporaryDirectory() as tmp:
            store = routing_store_for(self, Path(tmp) / 'routing.sqlite')
            route = store.upsert_sandbox(_sandbox_route(
                sandbox_id='live', state='running', node_id='node', job_id='job',
                node_url='http://node', node_epoch='boot',
            ))
            with store._transaction() as conn:
                conn.execute('CREATE TABLE redundant_writes (kind TEXT)')
                conn.execute("CREATE FUNCTION record_write() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN INSERT INTO redundant_writes VALUES (TG_ARGV[0]); RETURN NULL; END $$")
                conn.execute("CREATE TRIGGER spec_rewrite AFTER UPDATE OF spec_json ON sandboxes FOR EACH ROW EXECUTE FUNCTION record_write('spec')")
                conn.execute("CREATE TRIGGER generation_rewrite AFTER UPDATE ON sandbox_generation_hwm FOR EACH ROW EXECUTE FUNCTION record_write('generation')")
                conn.execute('INSERT INTO sandbox_storage_dependencies VALUES (?, ?, ?) ON CONFLICT (sandbox_id) DO UPDATE SET storage_snapshot_json = excluded.storage_snapshot_json', ('live', route.generation, '{"marker":"keep"}'))
            updated = store.set_sandbox_state_if_current(
                route, expected_states={'running'}, state='running', node_epoch='boot',
                activity_epoch=route.activity_epoch + 1,
            )
            self.assertEqual(updated.spec, route.spec)
            self.assertEqual(store.get_sandbox('live'), updated)
            with store._connect() as conn:
                self.assertEqual(conn.execute('SELECT * FROM redundant_writes').fetchall(), [])
                self.assertEqual(conn.execute('SELECT storage_snapshot_json FROM sandbox_storage_dependencies').fetchone()[0], '{"marker":"keep"}')

    def test_combined_warm_readiness_and_dispatch_preserve_first_timestamps(self):
        with TemporaryDirectory() as tmp:
            store = routing_store_for(self, Path(tmp) / 'routing.sqlite')
            route = store.upsert_sandbox(_sandbox_route(
                sandbox_id='live', state='running', node_id='node', job_id='job', node_url='http://node',
            ))
            first, changed = store.upsert_program_request_transition_with_change(
                route, request_id='request', rollout_id='rollout', state='waking',
                response_ready_at='2026-09-22T00:00:00+00:00',
                transition_at='2026-09-22T00:00:01+00:00',
            )
            self.assertTrue(changed)
            self.assertEqual(first, store.program_request_readonly('request'))
            self.assertEqual(first.response_ready_at, '2026-09-22T00:00:00+00:00')
            self.assertEqual(first.wake_started_at, '2026-09-22T00:00:01+00:00')
            retry, changed = store.upsert_program_request_transition_with_change(
                route, request_id='request', rollout_id='rollout', state='waking',
                response_ready_at='2026-09-22T01:00:00+00:00',
            )
            self.assertFalse(changed)
            self.assertEqual(retry, first)

    def test_inventory_batches_unchanged_routes_but_keeps_fences_and_dependencies(self):
        from dataclasses import replace
        from sandboxes.models import SandboxInventoryEntry, utc_now
        with TemporaryDirectory() as tmp:
            store = routing_store_for(self, Path(tmp) / 'routing.sqlite')
            routes = [store.upsert_sandbox(_sandbox_route(
                sandbox_id=f'live-{i}', state='running', node_id='node',
                job_id='job', node_url='http://node', node_epoch='boot',
            )) for i in range(64)]
            store.upsert_pending('live-0', routes[0].resources)
            observations = [SandboxInventoryEntry(
                r.sandbox_id, r.generation, r.create_operation_id, r.spec_hash,
                'parked' if i == 0 else 'running', r.resources, storage_dependency={},
            ) for i, r in enumerate(routes)]
            observed_at = utc_now().isoformat()
            kwargs = dict(node_id='node', job_id='job', node_epoch='boot',
                          activity_epoch=12, observed_at=observed_at,
                          reported_sandbox_ids=[r.sandbox_id for r in routes])
            with patch.object(store, '_write_sandbox', wraps=store._write_sandbox) as write:
                self.assertEqual(store.reconcile_sandboxes_for_node(
                    'http://node', observations, **kwargs,
                ), ([], []))
                self.assertEqual(write.call_count, 1)
            for i, route in enumerate(routes):
                self.assertEqual(store.get_sandbox(route.sandbox_id), replace(
                    route, state='parked' if i == 0 else 'running',
                    activity_epoch=12, updated_at=observed_at,
                ))
            self.assertEqual(store.pending_demand().pending_count, 0)
            with store._connect() as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM sandbox_storage_dependencies').fetchone()[0], 64)
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM sandbox_generation_hwm').fetchone()[0], 64)
            # A stale report must not move either state or watermark backwards.
            kwargs['activity_epoch'] = 11
            stale = [replace(o, state='running') for o in observations]
            store.reconcile_sandboxes_for_node('http://node', stale, **kwargs)
            self.assertEqual(store.get_sandbox('live-0').state, 'parked')
            self.assertEqual(store.get_sandbox('live-0').activity_epoch, 12)

    def test_failed_transaction_does_not_poison_next_borrower(self):
        with TemporaryDirectory() as tmp:
            store = routing_store_for(self, Path(tmp) / 'routing.sqlite')
            store.upsert_sandbox(_sandbox_route(sandbox_id='local', state='parked', node_id='node', job_id='job', node_url='http://node'))
            with self.assertRaisesRegex(RuntimeError, 'injected'):
                with store._transaction() as conn:
                    conn.execute("UPDATE sandboxes SET state='running'")
                    raise RuntimeError('injected')
            self.assertEqual(store.get_sandbox('local').state, 'parked')

    def test_warm_durable_wake_records_observation_with_outcome_only(self):
        from unittest.mock import Mock
        from sandboxes.control_plane import ControlPlaneHandler
        with TemporaryDirectory() as tmp:
            store = routing_store_for(self, Path(tmp) / 'routing.sqlite')
            route = store.upsert_sandbox(_sandbox_route(
                sandbox_id='live', state='running', node_id='node', job_id='job', node_url='http://node',
            ))
            handler = object.__new__(ControlPlaneHandler)
            handler.routing_store = store
            handler.metrics_store = Mock()
            payload = dict(request_id='request', rollout_id='rollout', durable_lifecycle=True)
            handler._prepare_program_lifecycle(route, 'wake', payload)
            self.assertIsNone(store.program_request_readonly('request'))
            started = handler._warm_program_wake_observation[3]
            handler._record_completed_program_lifecycle(route, 'wake', payload)
            committed = store.program_request_readonly('request')
            self.assertEqual(committed.state, 'acting')
            self.assertEqual(committed.response_ready_at, started)
            self.assertEqual(committed.wake_started_at, started)
            self.assertGreaterEqual(committed.wake_completed_at, started)
            # An exact replay retains the first timestamps and no-op semantics.
            handler._prepare_program_lifecycle(route, 'wake', payload)
            handler._record_completed_program_lifecycle(route, 'wake', payload)
            self.assertEqual(store.program_request_readonly('request'), committed)

    def test_failed_warm_wake_retains_demand_and_original_start_observation(self):
        from unittest.mock import Mock
        from sandboxes.control_plane import ControlPlaneHandler
        with TemporaryDirectory() as tmp:
            store = routing_store_for(self, Path(tmp) / 'routing.sqlite')
            route = store.upsert_sandbox(_sandbox_route(
                sandbox_id='live', state='running', node_id='node', job_id='job', node_url='http://node',
            ))
            handler = object.__new__(ControlPlaneHandler)
            handler.routing_store = store
            handler.metrics_store = Mock()
            payload = dict(request_id='request', rollout_id='rollout', durable_lifecycle=True)
            handler._prepare_program_lifecycle(route, 'wake', payload)
            started = handler._warm_program_wake_observation[3]
            program, changed = handler._record_program_request_transition(
                route, payload, state='waking', last_error='temporary node failure',
            )
            self.assertTrue(changed)
            self.assertEqual(program.wake_started_at, started)
            self.assertEqual(program.response_ready_at, started)
            self.assertEqual(program.last_error, 'temporary node failure')

    def test_failed_program_commit_cannot_acknowledge_partial_wake_confirmation(self):
        with TemporaryDirectory() as tmp:
            store = routing_store_for(self, Path(tmp) / 'routing.sqlite')
            route = store.upsert_sandbox(_sandbox_route(
                sandbox_id='live', state='running', node_id='node', job_id='job',
                node_url='http://node', node_epoch='boot', activity_epoch=10,
            ))
            with store._transaction() as conn:
                conn.execute("CREATE FUNCTION fail_program() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected projection failure'; END $$")
                conn.execute("CREATE TRIGGER fail_program BEFORE INSERT ON program_requests FOR EACH ROW EXECUTE FUNCTION fail_program()")
            args = dict(node_epoch='boot', activity_epoch=11,
                        program_transition=dict(request_id='request', rollout_id='rollout', state='acting'))
            with self.assertRaises(RoutingUnavailable):
                store.confirm_sandbox_wake(route, **args)
            self.assertEqual(store.get_sandbox('live'), route)
            self.assertIsNone(store.program_request_readonly('request'))
            with store._transaction() as conn:
                conn.execute('DROP TRIGGER fail_program ON program_requests')
            confirmed, (program, changed) = store.confirm_sandbox_wake(route, **args)
            self.assertEqual(confirmed.activity_epoch, 11)
            self.assertTrue(changed)
            self.assertEqual(program.state, 'acting')

    def test_stale_wake_proof_cannot_publish_program_success(self):
        with TemporaryDirectory() as tmp:
            store = routing_store_for(self, Path(tmp) / 'routing.sqlite')
            route = store.upsert_sandbox(_sandbox_route(
                sandbox_id='live', state='running', node_id='node', job_id='job',
                node_url='http://node', node_epoch='boot', activity_epoch=10,
            ))
            confirmed, result = store.confirm_sandbox_wake(
                route, node_epoch='old-boot', activity_epoch=11,
                program_transition=dict(request_id='request', rollout_id='rollout', state='acting'),
            )
            self.assertIsNone(confirmed)
            self.assertEqual(result, (None, False))
            self.assertIsNone(store.program_request_readonly('request'))
            self.assertEqual(store.get_sandbox('live'), route)
