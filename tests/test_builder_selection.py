import json
import unittest
from dataclasses import replace
from unittest.mock import Mock

from sandboxes.build_admission import BUILD_ADMISSION_CAPACITY_LABEL
from sandboxes.control_plane import ControlPlaneHandler, ProxiedResponse
from sandboxes.deployment import package_version
from sandboxes.models import NodeHeartbeat, ResourceQuantity, utc_now
from sandboxes.registry import heartbeat_to_dict
from tests.gateway_support import gateway_services


class BuilderSelectionTests(unittest.TestCase):
    def setUp(self):
        self.busy = NodeHeartbeat(
            node_id="a-busy",
            job_id="1",
            updated_at=utc_now(),
            active_sandboxes=0,
            active_image_builds=4,
            node_url="http://busy",
            agent_version=package_version(),
            deployment_id="test-deployment",
            capabilities=("image-build",),
            total_resources=ResourceQuantity(vcpu=16, memory_mb=49152, disk_mb=204800),
            physical_disk_free_mb=60000,
        )
        self.idle = replace(
            self.busy,
            node_id="b-idle",
            job_id="2",
            node_url="http://idle",
            active_image_builds=0,
            physical_disk_free_mb=240000,
        )
        self.handler = object.__new__(ControlPlaneHandler)
        self.handler.services = gateway_services()
        self.handler.services.fleet.ready_heartbeats = Mock(return_value=[self.busy, self.idle])
        self.handler._proxy_request = Mock(side_effect=self.probe)

    def probe(self, url, path, **kwargs):
        if path == "/v1/heartbeat":
            node = self.busy if url == self.busy.node_url else self.idle
            return self.response(200, {"heartbeat": heartbeat_to_dict(node)})
        return self.response(404)

    @staticmethod
    def response(status, payload=None):
        return ProxiedResponse(status, {}, json.dumps(payload or {}).encode())

    def test_unknown_owner_does_not_dispatch_duplicate_build(self):
        self.handler._proxy_request.side_effect = None
        for response in (self.response(503), self.response(200, {"build": {}})):
            with self.subTest(response=response.status):
                self.handler._proxy_request.return_value = response
                self.assertIsNone(
                    self.handler._select_builder_node(image_id="existing")
                )

    def test_terminal_build_keeps_its_owner_until_cleanup_releases_admission(self):
        for status in ("succeeded", "failed"):
            for phase in ("preparing_solving", "finishing"):
                with self.subTest(status=status, phase=phase):
                    self.handler._proxy_request.reset_mock()
                    self.handler._proxy_request.side_effect = None
                    self.handler._proxy_request.return_value = self.response(
                        200, {"build": {"status": status, "admission_phase": phase}})
                    self.assertEqual(
                        self.handler._select_builder_node(image_id="existing"), self.busy)
                    self.assertEqual(self.handler._proxy_request.call_count, 1)

    def test_live_capacity_can_reopen_or_close_a_periodically_full_builder(self):
        stale = replace(self.busy, labels={BUILD_ADMISSION_CAPACITY_LABEL: "4"})
        self.handler.services.fleet.ready_heartbeats.return_value = [stale]
        self.busy = replace(self.busy, labels={BUILD_ADMISSION_CAPACITY_LABEL: "6"})
        self.assertEqual(self.handler._select_builder_node(image_id="new"), self.busy)
        for capacity in ("4", "0", "bad"):
            with self.subTest(capacity=capacity):
                self.busy = replace(self.busy, labels={BUILD_ADMISSION_CAPACITY_LABEL: capacity})
                self.assertIsNone(self.handler._select_builder_node(image_id="new"))

    def test_live_draining_or_restarted_builder_is_not_selected(self):
        for changed in (
            replace(self.idle, draining=True),
            replace(self.idle, node_epoch="new"),
        ):
            with self.subTest(node=changed):
                self.handler._proxy_request.side_effect = [
                    self.response(404),
                    self.response(404),
                    self.probe(self.busy.node_url, "/v1/heartbeat"),
                    self.response(200, {"heartbeat": heartbeat_to_dict(changed)}),
                ]
                self.assertIsNone(self.handler._select_builder_node(image_id="new"))
