import re
import unittest

from sandboxes.metrics_prometheus import render_snapshot

_SAMPLE = re.compile(r'^(?P<name>[a-z_]+)(?:\{(?P<labels>.*)\})? (?P<value>\S+)$')
_LABEL = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


def parse(text: str) -> tuple[dict[tuple[str, frozenset], float], list[str]]:
    samples, types = {}, []
    for line in text.splitlines():
        if line.startswith("# TYPE "):
            types.append(line.split()[2])
        elif line and not line.startswith("#"):
            match = _SAMPLE.match(line)
            assert match, line
            labels = frozenset(_LABEL.findall(match["labels"] or ""))
            samples[(match["name"], labels)] = float(match["value"])
    return samples, types


def series(samples, name, **labels):
    return samples.get((name, frozenset(labels.items())))


SNAPSHOT = {
    "generated_at": "2026-10-30T12:00:00+00:00",
    "nodes": {
        "total": 2, "fresh": 2, "sandbox": 1, "sandbox_ready": 1, "builder": 1,
        "items": [
            {"node_id": "node:10.42.0.3", "job_id": "173527833", "capabilities": ["sandbox"],
             "agent_version": "0.10.0", "fresh": True, "age_seconds": 4, "draining": False,
             "admission_open": True, "active_sandboxes": 7,
             "total_resources": {"vcpu": 48.0, "memory_mb": 196608, "disk_mb": 522240},
             "used_resources": {"vcpu": 28.0, "memory_mb": 28672, "disk_mb": 1000},
             "free_resources": {"vcpu": 20.0, "memory_mb": 167936, "disk_mb": 521240},
             "actual_usage": {"collected_at": "2026-10-30T11:59:58+00:00", "cpu_percent": 12.5,
                              "memory_psi_full_avg10": None, "resource_evidence": {"x": 1}}},
            {"node_id": "node:10.42.0.4", "job_id": "173527900", "capabilities": ["image-build"],
             "agent_version": "0.10.0", "fresh": True, "draining": True, "admission_open": False},
        ],
    },
    "sandboxes": {
        "states": {"running": 5, "parked": 2}, "worker_states": {"attached": 6, "detached": 1},
        "pending": 3, "pending_resources": {"vcpu": 3.0, "memory_mb": 6144, "disk_mb": 30000},
        "oldest_pending_seconds": None,
    },
    "autoscaler": {
        "cycle": 41, "timestamp": "2026-10-30T11:59:55+00:00", "ready_nodes": 1, "provisioning_nodes": 1,
        "actions": [{"kind": "create", "count": 1}, {"kind": "create", "count": 2}, {"kind": "stop", "count": 1}],
        "builder_actions": [], "resource_deficit": {"vcpu": 2.5},
        "live_signals": {"cpu_utilization": 0.4, "newest_age_seconds": None},
        "effective_policy": {"max_nodes": 8, "min_nodes": 0},
    },
    "registry": {"ok": True, "disk": {"total_bytes": 500, "used_bytes": 10, "state": "ok"}},
    "telemetry": {"enabled": False},
}


class RenderSnapshotTests(unittest.TestCase):
    def test_fleet_state_becomes_labelled_gauges(self) -> None:
        samples, types = parse(render_snapshot(SNAPSHOT))

        self.assertEqual(series(samples, "ucloud_fleet_nodes", state="sandbox_ready"), 1)
        sandbox_node = {"job_id": "173527833", "role": "sandbox"}
        self.assertEqual(series(samples, "ucloud_fleet_node_active_sandboxes", **sandbox_node), 7)
        self.assertEqual(series(samples, "ucloud_fleet_node_resources", kind="free", resource="vcpu",
                                **sandbox_node), 20)
        self.assertEqual(series(samples, "ucloud_fleet_node_cpu_percent", **sandbox_node), 12.5)
        builder = {"job_id": "173527900", "role": "builder"}
        self.assertEqual(series(samples, "ucloud_fleet_node_draining", **builder), 1)
        self.assertEqual(series(samples, "ucloud_fleet_node_admission_open", **builder), 0)
        self.assertEqual(series(samples, "ucloud_fleet_sandboxes", state="parked"), 2)
        self.assertEqual(series(samples, "ucloud_fleet_sandbox_worker_states", worker_state="detached"), 1)
        self.assertEqual(series(samples, "ucloud_fleet_pending_resources", kind="capacity", resource="memory_mb"),
                         6144)
        self.assertEqual(series(samples, "ucloud_fleet_autoscaler_actions", role="sandbox", kind="create"), 3)
        self.assertEqual(series(samples, "ucloud_fleet_autoscaler_resources", kind="deficit", resource="vcpu"), 2.5)
        self.assertEqual(series(samples, "ucloud_fleet_autoscaler_policy", limit="min_nodes"), 0)
        self.assertEqual(series(samples, "ucloud_fleet_registry_disk_state", state="ok"), 1)
        self.assertEqual(series(samples, "ucloud_fleet_snapshot_timestamp_seconds"), 1793361600)
        self.assertEqual(len(types), len(set(types)))
        self.assertTrue(all(name.startswith("ucloud_fleet_") for name, _ in samples))

    def test_unknown_values_are_left_out_not_zeroed(self) -> None:
        samples, _ = parse(render_snapshot(SNAPSHOT))
        names = {name for name, _ in samples}

        self.assertNotIn("ucloud_fleet_pending_create_oldest_seconds", names)
        self.assertNotIn("ucloud_fleet_node_memory_psi_full_avg10", names)
        self.assertNotIn("ucloud_fleet_node_collected_at", names)
        self.assertIsNone(series(samples, "ucloud_fleet_autoscaler_signal", signal="newest_age_seconds"))
        self.assertEqual(series(samples, "ucloud_fleet_autoscaler_signal", signal="cpu_utilization"), 0.4)

    def test_label_values_are_escaped(self) -> None:
        snapshot = {"sandboxes": {"states": {'odd "state"\\\nx': 1}}}

        text = render_snapshot(snapshot)
        samples, _ = parse(text)

        self.assertIn('state="odd \\"state\\"\\\\\\nx"', text)
        self.assertEqual(len([key for key in samples if key[0] == "ucloud_fleet_sandboxes"]), 1)

    def test_empty_snapshot_renders(self) -> None:
        samples, types = parse(render_snapshot({}))

        self.assertEqual(samples, {})
        self.assertEqual(types, [])


if __name__ == "__main__":
    unittest.main()
