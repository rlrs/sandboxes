import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from tests import test_control_plane as fixtures
from sandboxes.control_plane import ControlPlaneHandler, lifecycle_node_payload
from sandboxes.node_agent import NodeAgentHandler
from sandboxes.sandbox import WarmParkDeferred


def hint():
    """A relay's model-wait advice (resource phase)."""
    return dict(sequence=1, phase="model_wait", ttl_seconds=10, expected_remaining_wait_seconds=100,
                observed_at=1000.0, evaluated_at=1000.0, expires_at=1010.0,
                registration_incarnation="a" * 64)


class ResourcePhaseForwardingTests(TestCase):
    def test_gateway_forwards_hint_only_on_bound_park(self):
        payload = dict(
            operation_id="park:r",
            durable_lifecycle=True,
            request_id="r",
            resource_phase=hint(),
        )
        result = lifecycle_node_payload(3, "park", payload)
        self.assertEqual(result, {"operation_id": "park:r", "relay_request_id": "r", "generation": 3,
                                  "resource_phase": payload["resource_phase"]})
        self.assertNotIn("resource_phase", lifecycle_node_payload(3, "wake", payload))
        unbound = {key: value for key, value in payload.items() if key != "durable_lifecycle"}
        self.assertNotIn("resource_phase", lifecycle_node_payload(3, "park", unbound))
        with self.assertRaisesRegex(ValueError, "background"):
            lifecycle_node_payload(3, "park", {**payload, "background": "yes"})

    def test_node_validates_advice_before_forwarding_it_under_existing_identity(self):
        handler = object.__new__(NodeAgentHandler)
        handler._write_json = Mock()
        handler._write_exception = Mock()
        park = Mock(side_effect=WarmParkDeferred(1))
        handler.manager = SimpleNamespace(park_with_activity_revision=park)
        payload = dict(
            operation_id="park:r",
            relay_request_id="r",
            generation=1,
            resource_phase=hint(),
        )
        handler._read_json_body = lambda: payload
        handler._park_sandbox("/v1/sandboxes/agent/park")
        self.assertEqual(
            park.call_args.kwargs["resource_phase"], payload["resource_phase"]
        )
        self.assertEqual(park.call_args.kwargs["generation"], 1)
        self.assertEqual(park.call_args.kwargs["relay_request_id"], "r")
        for mutation in (
            {"generation": 2.5},
            {"relay_request_id": None},
            {
                "resource_phase": {
                    **hint(),
                    "extra": 1,
                }
            },
        ):
            park.reset_mock()
            handler._read_json_body = lambda mutation=mutation: {**payload, **mutation}
            handler._park_sandbox("/v1/sandboxes/agent/park")
            park.assert_not_called()
        handler._read_json_body = lambda: {
            "operation_id": "park:r",
            "resource_phase": payload["resource_phase"],
        }
        handler._park_sandbox("/v1/sandboxes/agent/park")
        park.assert_not_called()

    def test_gateway_rejects_unbound_or_wake_phase_hint(self):
        handler = object.__new__(ControlPlaneHandler)
        handler._write_json = Mock()
        route = fixtures._sandbox_route(
            sandbox_id="agent", node_id="node", job_id="node", node_url="http://node"
        )
        for action, extra in [
            ("park", {}),
            ("wake", {"durable_lifecycle": True, "request_id": "r"}),
        ]:
            payload = dict(
                operation_id="op:r",
                resource_phase=hint(),
                **extra,
            )
            self.assertIsNone(
                handler._parse_lifecycle_request(
                    route, action, json.dumps(payload).encode()
                )
            )
