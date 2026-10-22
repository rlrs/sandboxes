"""Route accounting and the reservation that wakes and migrations place under."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable

from ..capabilities import (
    DISK_QUOTA_CAPABILITY, ENVIRONMENT_CONTRACT_CAPABILITY, ENVIRONMENT_RAFS_CAPABILITY,
    ENVIRONMENT_ROOT_CAPABILITY, HIBERNATE_LOCAL_CAPABILITY, MANAGED_PRIMARY_CAPABILITY,
    STATIC_FILE_MANAGEMENT_CAPABILITY,
)
from ..models import NodeHeartbeat, ResourceQuantity
from ..network_policy import SandboxNetworkPolicy
from ..placement_accounting import (
    PlacementRecord, PlacementReservation, _node_available_resources, _route_targets_node,
)
from ..resource_admission import node_accepts_dynamic_request
from ..routing import RoutingStore, SandboxRoute
from ..telemetry import Telemetry
from ..wake_placement import (
    WakeCapacityRefreshRequired, WakePlacementStopped, WakeSnapshotPublicationRequired,
)


class Placement:
    """Committed route accounting and the placement transaction that wakes
    and migrations reserve under. One instance per server, shared by every
    request thread.
    """

    def __init__(self, routing_store: RoutingStore, *, telemetry: Telemetry) -> None:
        self.routing_store = routing_store
        self.telemetry = telemetry

    def routes(self) -> list[PlacementRecord]:
        """Include in-flight destination imports in normal node admission."""

        routes: list[PlacementRecord] = list(self.routing_store.placement_routes_readonly())
        routes_by_id = {route.sandbox_id: route for route in routes}
        routes.extend(migration_reservations(self.routing_store, routes_by_id.get))
        return routes

    def routes_for_node(self, heartbeat: NodeHeartbeat) -> list[PlacementRecord]:
        """Read fresh owner admission state, including incoming migrations."""

        routes: list[PlacementRecord] = list(
            self.routing_store.sandbox_routes_matching_node_identity(
                node_id=heartbeat.node_id, job_id=heartbeat.job_id,
                node_url=heartbeat.node_url or "",
            )
        )
        by_id = {route.sandbox_id: route for route in routes}
        for migration in self.routing_store.sandbox_migrations(
            active_only=True,
            destination_identity=(heartbeat.node_id, heartbeat.job_id, heartbeat.node_url or ""),
        ):
            destination = PlacementReservation(
                reservation_id=migration.migration_id,
                node_id=migration.destination_node_id,
                job_id=migration.destination_job_id,
                node_url=migration.destination_node_url,
                resources=ResourceQuantity(), image="",
            )
            if not _route_targets_node(destination, heartbeat):
                continue
            source = by_id.get(migration.sandbox_id)
            if source is None:
                source = self.routing_store.get_sandbox_readonly(migration.sandbox_id)
            if source is None:
                continue
            resources = source.resources
            if migration.phase in {"routed", "activated"}:
                resources = ResourceQuantity(
                    vcpu=resources.vcpu, memory_mb=resources.memory_mb,
                )
            routes.append(replace(
                destination, resources=resources,
                image=str(source.spec.get("image") or ""),
            ))
        return routes

    def atomic(self, operation, *, worker_id=None):
        """Run a placement inside one routing transaction (see run_placement)."""
        with self.telemetry.span("gateway.placement.transaction"):
            return self.routing_store.run_placement(operation, worker_id=worker_id, outcomes=(
                WakePlacementStopped, WakeCapacityRefreshRequired, WakeSnapshotPublicationRequired,
            ))


def migration_reservations(
    routing_store: RoutingStore, source_route: Callable[[str], SandboxRoute | None],
) -> list[PlacementReservation]:
    """Incoming migrations' destination charges, which heartbeats lag."""

    reservations = []
    for migration in routing_store.sandbox_migrations(active_only=True):
        source = source_route(migration.sandbox_id)
        if source is None:
            continue
        # Before route commit the destination may already be allocating
        # quota and restoring metadata, while its heartbeat still has no
        # observation. Reserve the complete shape. After route commit the
        # parked route owns disk itself, but a wake relocation still needs
        # its CPU/RAM reservation through activation. Completion can then
        # atomically turn that parked route into ``waking``.
        reservation = source.resources
        if migration.phase in {"routed", "activated"}:
            reservation = ResourceQuantity(
                vcpu=source.resources.vcpu, memory_mb=source.resources.memory_mb,
            )
        reservations.append(PlacementReservation(
            reservation_id=migration.migration_id, node_id=migration.destination_node_id,
            job_id=migration.destination_job_id, node_url=migration.destination_node_url,
            resources=reservation, image=str(source.spec.get("image") or ""),
        ))
    return reservations


def _sandbox_required_capabilities(spec: dict[str, Any]) -> tuple[str, ...]:
    capabilities = []
    policy = SandboxNetworkPolicy.from_dict(spec.get("network_policy", {}))
    if policy.egress == "relay":
        capabilities.append(policy.capability)
    if bool(spec.get("parkable")):
        capabilities.extend((HIBERNATE_LOCAL_CAPABILITY, DISK_QUOTA_CAPABILITY))
        if bool(spec.get("managed_process")):
            capabilities.append(MANAGED_PRIMARY_CAPABILITY)
    filesystem = spec.get("filesystem") or {}
    security = spec.get("security") or {}
    if (
        spec.get("profile") == "linux_session"
        or spec.get("required_features")
        or spec.get("dns_servers")
        or security.get("supplementary_groups")
        or filesystem.get("shm_mb", 64) != 64
        or filesystem.get("workspace_storage") is not None
        or filesystem.get("management_helper", "shell") != "shell"
    ):
        capabilities.append(ENVIRONMENT_CONTRACT_CAPABILITY)
    if filesystem.get("management_helper") == "static":
        capabilities.append(STATIC_FILE_MANAGEMENT_CAPABILITY)
    if spec.get("environment_root"):
        # Chunk store M2: dispatched roots go to workers that honour them and
        # read RAFS, also for retries and moves, which re-read the route's spec.
        capabilities.extend((ENVIRONMENT_ROOT_CAPABILITY, ENVIRONMENT_RAFS_CAPABILITY))
    return tuple(capabilities)


def _node_can_fit(
    heartbeat: NodeHeartbeat,
    requested: ResourceQuantity,
    routes: list[PlacementRecord],
) -> bool:
    return _node_can_fit_available(
        heartbeat,
        requested,
        _node_available_resources(heartbeat, routes),
    )


def _node_can_fit_available(
    heartbeat: NodeHeartbeat,
    requested: ResourceQuantity,
    available: ResourceQuantity,
) -> bool:
    return node_accepts_dynamic_request(heartbeat, requested, available)


def _has_resource_values(resources: ResourceQuantity) -> bool:
    return resources.vcpu > 0 or resources.memory_mb > 0 or resources.disk_mb > 0
