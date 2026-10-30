"""The operator metrics snapshot as Prometheus text (GET /v1/metrics/prometheus).

VictoriaMetrics scrapes this so Grafana can chart fleet state over time. Every
series is a gauge named ``ucloud_fleet_*``; a value the snapshot does not have
(None, missing) is left out rather than reported as 0.
"""
from __future__ import annotations

from datetime import datetime
import math
from typing import Any

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
PREFIX = "ucloud_fleet_"
RESOURCES = ("vcpu", "memory_mb", "disk_mb")


class _Exposition:
    def __init__(self) -> None:
        self._families: dict[str, tuple[str, list[tuple[dict[str, str], float]]]] = {}

    def add(self, name: str, help_text: str, value: Any, **labels: Any) -> None:
        if isinstance(value, bool):
            value = int(value)
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            return
        family = self._families.setdefault(PREFIX + name, (help_text, []))
        family[1].append(({key: str(item) for key, item in labels.items()}, value))

    def resources(self, name: str, help_text: str, quantity: Any, **labels: Any) -> None:
        if isinstance(quantity, dict):
            for resource in RESOURCES:
                self.add(name, help_text, quantity.get(resource), resource=resource, **labels)

    def render(self) -> str:
        lines: list[str] = []
        for name, (help_text, samples) in self._families.items():
            lines += [f"# HELP {name} {help_text}", f"# TYPE {name} gauge"]
            for labels, value in samples:
                rendered = ",".join(f'{key}="{_escape(item)}"' for key, item in labels.items())
                lines.append(f"{name}{{{rendered}}} {_number(value)}" if rendered else f"{name} {_number(value)}")
        return "\n".join(lines) + "\n"


def render_snapshot(snapshot: dict[str, Any]) -> str:
    out = _Exposition()
    out.add("snapshot_timestamp_seconds", "When the gateway built this snapshot",
            _timestamp(snapshot.get("generated_at")))
    _nodes(out, snapshot.get("nodes") or {})
    for scope, pool in (snapshot.get("resources") or {}).items():
        out.add("pool_nodes", "Fresh nodes in a resource pool", pool.get("nodes"), pool=scope)
        for kind in ("total", "used", "free"):
            out.resources("pool_resources", "Resources of a pool's nodes", pool.get(kind), pool=scope, kind=kind)
    _sandboxes(out, snapshot.get("sandboxes") or {})
    capacity = snapshot.get("capacity") or {}
    out.add("prepared_capacity_requests", "Live capacity prepare requests", capacity.get("prepared"))
    out.add("prepared_capacity_sandboxes", "Sandboxes announced by prepare requests", capacity.get("prepared_sandboxes"))
    out.resources("prepared_capacity_resources", "Resources announced by prepare requests",
                  capacity.get("prepared_resources"))
    _programs(out, snapshot.get("programs") or {})
    images = snapshot.get("images") or {}
    out.add("image_builds_pending", "Image builds waiting for a builder", images.get("pending_builds"))
    out.add("image_builds_active", "Image builds running on builders", images.get("active_builds"))
    out.add("image_build_oldest_pending_seconds", "Age of the oldest waiting image build",
            images.get("oldest_pending_build_seconds"))
    builders = snapshot.get("builders") or {}
    out.add("prepared_builders", "Builders announced by prepare requests", builders.get("prepared_builders"))
    scale_up = snapshot.get("scale_up") or {}
    for quantile in ("p50", "p95", "max"):
        out.add("scale_up_wait_milliseconds", "Recent create waits for a new worker",
                scale_up.get(f"{quantile}_ms"), quantile=quantile)
    _autoscaler(out, snapshot.get("autoscaler"))
    _registry(out, snapshot.get("registry") or {})
    telemetry = snapshot.get("telemetry") or {}
    for kind in ("accepted", "dropped", "exported"):
        out.add("telemetry_spans", "Spans this gateway process handled", telemetry.get(f"{kind}_spans"), kind=kind)
    out.add("telemetry_failed_exports", "Failed telemetry exports from this gateway process",
            telemetry.get("failed_exports"))
    return out.render()


def _nodes(out: _Exposition, nodes: dict[str, Any]) -> None:
    for state in ("total", "fresh", "compatible", "incompatible", "sandbox", "sandbox_ready",
                  "sandbox_draining", "sandbox_admission_closed", "builder"):
        out.add("nodes", "Nodes by heartbeat state", nodes.get(state), state=state)
    for item in nodes.get("items") or []:
        labels = {"job_id": item.get("job_id") or item.get("node_id") or "",
                  "role": _role(item.get("capabilities") or [])}
        out.add("node_info", "A node that has sent a heartbeat", 1, agent_version=item.get("agent_version") or "",
                node_id=item.get("node_id") or "", **labels)
        out.add("node_fresh", "The node's heartbeat is within the TTL", item.get("fresh"), **labels)
        out.add("node_heartbeat_age_seconds", "Seconds since the node's last heartbeat", item.get("age_seconds"),
                **labels)
        out.add("node_draining", "The node is draining", item.get("draining"), **labels)
        out.add("node_admission_open", "The node admits new work", item.get("admission_open"), **labels)
        for field in ("active_sandboxes", "active_image_builds", "active_sandbox_creates"):
            out.add(f"node_{field}", f"The node's {field.replace('_', ' ')}", item.get(field), **labels)
        for kind in ("total", "used", "free"):
            out.resources("node_resources", "The node's admitted resources", item.get(f"{kind}_resources"),
                          kind=kind, **labels)
        for field, value in (item.get("actual_usage") or {}).items():
            out.add(f"node_{field}", f"Node runtime measurement {field}", value, **labels)


def _sandboxes(out: _Exposition, sandboxes: dict[str, Any]) -> None:
    for state, count in (sandboxes.get("states") or {}).items():
        out.add("sandboxes", "Sandbox routes by state", count, state=state)
    for state, count in (sandboxes.get("worker_states") or {}).items():
        out.add("sandbox_worker_states", "Sandbox routes by worker attachment", count, worker_state=state)
    for kind in ("running", "active_routes", "routes_on_fresh_nodes", "portable_parked_routes",
                 "provisional_running_routes", "stale_routes"):
        out.add("sandbox_routes", "Sandbox route counts", sandboxes.get(kind), kind=kind)
    for kind, prefix in (("capacity", ""), ("suppressed", "suppressed_")):
        out.add("pending_creates", "Creates waiting for capacity", sandboxes.get(f"{prefix}pending"), kind=kind)
        out.add("pending_create_attempts", "Placement attempts of waiting creates",
                sandboxes.get(f"{prefix}pending_attempts"), kind=kind)
        out.resources("pending_resources", "Resources of waiting creates",
                      sandboxes.get(f"{prefix}pending_resources"), kind=kind)
    out.add("pending_create_oldest_seconds", "Age of the oldest create waiting for capacity",
            sandboxes.get("oldest_pending_seconds"))


def _programs(out: _Exposition, programs: dict[str, Any]) -> None:
    for kind in ("requests", "rollouts", "sandboxes"):
        out.add("programs", "Open program requests", programs.get(kind), kind=kind)
    for state, count in (programs.get("states") or {}).items():
        out.add("program_states", "Program requests by state", count, state=state)
    for name in ("model_wait_p50_ms", "model_wait_p95_ms", "response_to_wake_p50_ms", "response_to_wake_p95_ms",
                 "oldest_model_wait_seconds", "oldest_ready_to_wake_seconds"):
        out.add(f"program_{name}", f"Program {name.replace('_', ' ')}", programs.get(name))


def _autoscaler(out: _Exposition, cycle: Any) -> None:
    if not isinstance(cycle, dict):
        return
    out.add("autoscaler_cycle", "Number of the latest autoscaler cycle", cycle.get("cycle"))
    out.add("autoscaler_cycle_timestamp_seconds", "When the latest autoscaler cycle ran",
            _timestamp(cycle.get("timestamp")))
    for state in ("ready", "provisioning", "unreachable", "total"):
        out.add("autoscaler_nodes", "Nodes the autoscaler counted", cycle.get(f"{state}_nodes"), state=state)
    out.add("autoscaler_pending", "Creates the autoscaler counted as demand", cycle.get("pending_count"))
    for kind in ("pending", "suppressed_pending", "prepared", "desired", "projected_free"):
        out.resources("autoscaler_resources", "Resources in the latest autoscaler decision",
                      cycle.get(f"{kind}_resources"), kind=kind)
    out.resources("autoscaler_resources", "Resources in the latest autoscaler decision",
                  cycle.get("resource_deficit"), kind="deficit")
    for role, key in (("sandbox", "actions"), ("builder", "builder_actions")):
        counts: dict[str, int] = {}
        for action in cycle.get(key) or []:
            if isinstance(action, dict):
                kind = str(action.get("kind") or "unknown")
                counts[kind] = counts.get(kind, 0) + int(action.get("count") or 0)
        for kind, count in counts.items():
            out.add("autoscaler_actions", "Nodes the latest decision creates or stops", count, role=role, kind=kind)
    out.add("autoscaler_pressure_scale_up", "Live pressure asked for another node", cycle.get("pressure_scale_up"))
    out.add("autoscaler_create_pressure_scale_up", "Create pressure asked for another node",
            cycle.get("create_pressure_scale_up"))
    for signal, value in (cycle.get("live_signals") or {}).items():
        out.add("autoscaler_signal", "Live scale signal in the latest decision", value, signal=signal)
    policy = cycle.get("effective_policy") or {}
    for name in ("max_nodes", "min_nodes", "max_provisioning_nodes"):
        out.add("autoscaler_policy", "Effective scale policy limit", policy.get(name), limit=name)


def _registry(out: _Exposition, registry: dict[str, Any]) -> None:
    out.add("registry_ok", "The private registry answered", registry.get("ok"))
    out.add("registry_repositories", "Repositories in the private registry", registry.get("repository_count"))
    disk = registry.get("disk") or {}
    for kind in ("total", "used", "available"):
        out.add("registry_disk_bytes", "Private registry disk", disk.get(f"{kind}_bytes"), kind=kind)
    out.add("registry_disk_used_percent", "Private registry disk used", disk.get("used_percent"))
    if disk.get("state"):
        out.add("registry_disk_state", "Private registry disk state", 1, state=disk["state"])


def _role(capabilities: list[str]) -> str:
    return "builder" if "image-build" in capabilities else "sandbox" if "sandbox" in capabilities else "other"


def _timestamp(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return None


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() and abs(value) < 2**53 else repr(float(value))
