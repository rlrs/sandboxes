from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest

from sandboxes.exec_routing import (
    ExecRoutingService,
    ExecRouteUnavailable,
    heartbeat_proves_route_absent,
)
from sandboxes.exec_session_routes import (
    MAX_WORKER_ID_BYTES,
    ExecSessionRoutes,
    SignedExecRoute,
    valid_session_prefix,
    worker_id_fits,
)
from sandboxes.models import NodeHeartbeat, ResourceQuantity, SandboxInventoryEntry

ROUTE = SignedExecRoute(
    sandbox_id="rollout-7f3c",
    sandbox_generation=3,
    node_id="node-a",
    job_id="job-1",
    issued_at=1_790_000_000,
)
NODE_SUFFIX = "0123456789abcdef0123456789abcdef"


class ExecSessionRoutesTests(unittest.TestCase):
    def test_round_trip_binds_incarnation_and_worker(self) -> None:
        routes = ExecSessionRoutes("gateway-secret")
        prefix = routes.prefix(ROUTE)

        self.assertTrue(valid_session_prefix(prefix))
        self.assertEqual(routes.decode(f"{prefix}.{NODE_SUFFIX}"), ROUTE)
        self.assertEqual(
            routes.decode(f"{prefix}.{NODE_SUFFIX}").issued_at_iso,
            "2026-09-21T14:13:20+00:00",
        )

    def test_routed_and_malformed_names_are_not_signed_routes(self) -> None:
        routes = ExecSessionRoutes("gateway-secret")
        prefix = routes.prefix(ROUTE)

        for session_id in (
            f"exec-{NODE_SUFFIX}",
            prefix,
            f"{prefix}.{NODE_SUFFIX.upper()}",
            f"{prefix}.{NODE_SUFFIX}0",
            f"{prefix}.{NODE_SUFFIX}/../x",
            "",
        ):
            with self.subTest(session_id=session_id):
                self.assertIsNone(routes.decode(session_id))

    def test_signature_rejects_forgery_and_rotated_secret(self) -> None:
        routes = ExecSessionRoutes("gateway-secret")
        version, payload, mac = routes.prefix(ROUTE).split(".")
        forged = ExecSessionRoutes("other").prefix(
            SignedExecRoute("rollout-7f3c", 3, "node-b", "job-2", ROUTE.issued_at)
        )
        _, forged_payload, _ = forged.split(".")

        self.assertIsNone(
            routes.decode(f"{version}.{forged_payload}.{mac}.{NODE_SUFFIX}")
        )
        self.assertIsNone(ExecSessionRoutes("rotated").decode(
            f"{version}.{payload}.{mac}.{NODE_SUFFIX}"
        ))

    def test_rejects_identity_that_cannot_fit_a_prefix(self) -> None:
        routes = ExecSessionRoutes("gateway-secret")
        with self.assertRaises(ValueError):
            routes.prefix(
                SignedExecRoute("s" * 600, 1, "node-a", "job-1", ROUTE.issued_at)
            )

    def test_every_worker_id_heartbeat_intake_accepts_fits_a_prefix(self) -> None:
        routes = ExecSessionRoutes("gateway-secret")
        # JSON escapes a control character to six bytes; UTF-8 needs up to four.
        for worst in ("\x01" * (MAX_WORKER_ID_BYTES // 6), "\U0001f600" * (MAX_WORKER_ID_BYTES // 4),
                      "n" * MAX_WORKER_ID_BYTES):
            with self.subTest(length=len(worst)):
                self.assertTrue(worker_id_fits(worst))
                prefix = routes.prefix(
                    SignedExecRoute("s" * 64, 2**63 - 1, worst, worst, 2**40)
                )
                self.assertTrue(valid_session_prefix(prefix))
        self.assertFalse(worker_id_fits("n" * (MAX_WORKER_ID_BYTES + 1)))
        self.assertFalse(worker_id_fits("\x01" * (MAX_WORKER_ID_BYTES // 6 + 1)))
        self.assertFalse(worker_id_fits("\u00e9" * (MAX_WORKER_ID_BYTES // 2 + 1)))

    def test_worker_shape_check(self) -> None:
        self.assertFalse(valid_session_prefix("exec-abc"))
        self.assertFalse(valid_session_prefix("xr1.payload.short"))
        self.assertFalse(valid_session_prefix("xr1.a b.AAAAAAAAAAAAAAAAAAAAAA"))
        self.assertTrue(valid_session_prefix("xr1.YQ.AAAAAAAAAAAAAAAAAAAAAA"))

    def test_requires_secret(self) -> None:
        with self.assertRaises(ValueError):
            ExecSessionRoutes(" ")


class _Routing:
    def __init__(self, route=None) -> None:
        self.route = route

    def get_sandbox_readonly(self, sandbox_id):
        return self.route


class SignedExecRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.routes = ExecSessionRoutes("gateway-secret")
        issued = int((datetime.now(timezone.utc) - timedelta(seconds=5)).timestamp())
        self.signed = SignedExecRoute("one", 3, "node-a", "job-1", issued)
        self.session_id = f"{self.routes.prefix(self.signed)}.{NODE_SUFFIX}"

    def service(self, heartbeat, routing) -> ExecRoutingService:
        control = SimpleNamespace(
            get_heartbeat=lambda job_id, include_inventory=True: heartbeat
        )
        return ExecRoutingService(control, routing, 120, session_routes=self.routes)

    def unavailable(self, heartbeat, routing) -> ExecRouteUnavailable:
        with self.assertRaises(ExecRouteUnavailable) as raised:
            self.service(heartbeat, routing).resolve(self.session_id)
        return raised.exception

    def test_moved_or_detached_owner_reports_the_session_lost(self) -> None:
        for job_id, state in (("job-2", "attached"), ("job-1", "detached")):
            with self.subTest(job_id=job_id, state=state):
                route = SimpleNamespace(
                    generation=3, job_id=job_id, worker_state=state, updated_at="t"
                )
                error = self.unavailable(None, _Routing(route))
                self.assertEqual(error.status, 410)
                self.assertEqual(error.payload["error_code"], "exec_worker_lost")
                self.assertEqual(error.payload["sandbox_generation"], 3)

    def test_unsigned_session_names_are_unknown(self) -> None:
        for session_id in (f"exec-{NODE_SUFFIX}", self.session_id.replace("xr1.", "xr2.")):
            with self.subTest(session_id=session_id), self.assertRaises(ExecRouteUnavailable) as raised:
                self.service(None, _Routing()).resolve(session_id)
            self.assertEqual(
                (raised.exception.status, raised.exception.payload),
                (404, {"error": "exec route not found", "retryable": False}),
            )

    def test_refuses_an_exec_whose_route_cannot_be_signed(self) -> None:
        service = self.service(None, _Routing())
        route = SimpleNamespace(
            sandbox_id="one", generation=3, node_id="n" * 600, job_id="job-1"
        )
        with self.assertRaises(ExecRouteUnavailable) as raised:
            service.signed_prefix(route)
        self.assertEqual(
            (raised.exception.status, raised.exception.payload["error_code"]),
            (500, "exec_route_unsignable"),
        )
        self.assertTrue(
            service.signed_prefix(
                SimpleNamespace(sandbox_id="one", generation=3, node_id="a", job_id="j")
            ).startswith("xr1.")
        )



class RouteAbsenceTests(unittest.TestCase):
    def test_only_a_fresh_empty_inventory_proves_a_route_absent(self) -> None:
        now = datetime.now(timezone.utc)
        routed_at = (now - timedelta(seconds=5)).isoformat()
        empty = NodeHeartbeat(
            node_id="node-a", job_id="job-1", updated_at=now, active_sandboxes=0,
            received_at=now, inventory_complete=True,
        )
        parked = SandboxInventoryEntry(
            sandbox_id="one", generation=1, operation_id="create-1", spec_hash="a" * 64,
            state="parked", resources=ResourceQuantity(vcpu=1, memory_mb=256),
        )

        def absent(heartbeat) -> bool:
            return heartbeat_proves_route_absent(
                heartbeat, sandbox_id="one", route_created_at=routed_at,
                route_updated_at=routed_at, heartbeat_ttl_seconds=120,
            )

        self.assertTrue(absent(empty))
        # Zero active sandboxes does not mean a parked one disappeared.
        self.assertFalse(absent(replace(empty, inventory=(parked,))))
        self.assertFalse(absent(replace(empty, active_sandboxes=1)))
        self.assertFalse(absent(replace(empty, received_at=now - timedelta(seconds=10))))
        self.assertFalse(absent(None))


if __name__ == "__main__":
    unittest.main()
