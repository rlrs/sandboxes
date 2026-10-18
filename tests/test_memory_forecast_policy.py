from dataclasses import replace
from datetime import timedelta
import unittest

from sandboxes.models import (
    NodeRuntimeMetrics, ResourceQuantity, SandboxDemand,
    SandboxInventoryEntry, SandboxPlacementRequest, ScalePolicy, utc_now,
)
from sandboxes.policy import evaluate_scale
from tests.test_policy import node


class MemoryForecastPolicyTests(unittest.TestCase):
    def setUp(self):
        self.now = utc_now()
        self.total = ResourceQuantity(vcpu=32,memory_mb=98304,disk_mb=1449984)
        self.policy = ScalePolicy(max_nodes=8,max_create_per_cycle=8,
            max_provisioning_nodes=8, default_node_resources=self.total)

    def worker(self, identity, memory=0, active=1, **kwargs):
        return node(identity, total_resources=self.total, active=active,
            job_cpu=32, job_memory_gb=96,
            runtime_metrics=NodeRuntimeMetrics(collected_at=self.now,
                memory_total_mb=89900,memory_available_mb=89900-memory,
                memory_working_set_mb=memory), **kwargs)

    def pending(self, count):
        return SandboxDemand(placement_requests=(SandboxPlacementRequest(count=count,
            resources=ResourceQuantity(vcpu=8,memory_mb=2048,disk_mb=4096)),))

    def test_stale_complete_inventory_counts_transition_promise_once(self):
        worker = self.worker('transition',memory=20000)
        item = SandboxInventoryEntry('starting',1,'create','a'*64,'creating',
                                     ResourceQuantity(memory_mb=2048))
        worker = replace(worker,heartbeat=replace(worker.heartbeat,
            inventory_complete=True,inventory=(item,),reserved_resources=ResourceQuantity(memory_mb=2048),
            runtime_metrics=replace(worker.heartbeat.runtime_metrics,
                collected_at=self.now-timedelta(seconds=31))))
        decision = evaluate_scale([worker],SandboxDemand(),self.policy,now=self.now)
        self.assertEqual(decision.desired_resources.memory_mb,2560)
