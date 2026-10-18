"""Assemble one direct node agent, in the test process or in its own.

``FleetNode`` uses this in process; ``node_process.py`` uses it in a child
process the test can SIGKILL. It imports no harness module, so the child
loads only node-side code. The caller supplies the fakes that differ:
the image store and, where ``os.pidfd_open`` is missing, the fencer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path

from sandboxes.direct_oci import DirectOciConfigBuilder
from sandboxes.direct_provisioner import DirectSandboxProvisioner
from sandboxes.direct_registry import DirectSandboxRegistry
from sandboxes.direct_service import DirectSandboxService
from sandboxes.direct_warden import (
    DirectRunscWarden,
    DirectRunscWardenConfig,
    SubprocessCommandRunner,
)
from sandboxes.disk_claims import DiskClaimPolicy
from sandboxes.environment_artifact import EnvironmentArtifactRegistry
from sandboxes.environment_backend import EnvironmentBackendClient
from sandboxes.environment_manifest import HOST_EROFS_ABI
from sandboxes.environment_rootfs import EnvironmentRootfsStore
from sandboxes.hibernation import HibernationRuntimeFingerprint
from sandboxes.image_rootfs import OverlayRootfsManager
from sandboxes.images import DockerImageRuntime
from sandboxes.managed_registry import RegistryClient
from sandboxes.models import NodeRuntimeMetrics, ResourceQuantity, utc_now
from sandboxes.heartbeat_sender import HeartbeatSenderConfig
from sandboxes.node_agent import build_direct_node_agent_server
from sandboxes.runtime_metrics import SingleFlightRuntimeMetricsSampler
from sandboxes.storage_native_daemon import StorageNativeNodeClient

HARNESS = Path(__file__).resolve().parent


def _canonical_sha256(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("ascii")
    ).hexdigest()


# A fake pool of NBD devices: the image store's device budget and metrics.
ENVIRONMENT_BLOCK_DEVICES = 16


def fingerprint(rootfs_format: str) -> HibernationRuntimeFingerprint:
    """One fleet-wide fingerprint per image store, like one pinned runsc on
    every node: nodes stay migration-compatible although each has its own
    wrapper file. The rootfs format is ``build_direct_runtime_service``'s."""
    return HibernationRuntimeFingerprint(
        runsc_sha256=hashlib.sha256((HARNESS / "fake_runsc.py").read_bytes()).hexdigest(),
        runsc_commit="f" * 40,
        platform="systrap",
        architecture=os.uname().machine,
        page_size=os.sysconf("SC_PAGE_SIZE"),
        cpu_features_sha256=hashlib.sha256(b"fake-runsc-cpu").hexdigest(),
        boot_config_sha256=_canonical_sha256({
            "network": "none",
            "platform": "systrap",
            "rootfs_format": rootfs_format,
            "quota_layout": "storage-native-v1",
            "runtime": "fake-runsc-v1",
        }),
        rootfs_sha256="0" * 64,
    )


FINGERPRINT = fingerprint("ucloud-overlay2-rootfs-v1")
ENVIRONMENT_FINGERPRINT = fingerprint(HOST_EROFS_ABI)


def fixed_metrics() -> NodeRuntimeMetrics:
    return NodeRuntimeMetrics(
        collected_at=utc_now(),
        cpu_percent=0.0,
        cpu_count=8,
        load_average_1m=0.0,
        memory_total_mb=65536,
        memory_available_mb=60000,
    )


@dataclass(frozen=True)
class NodeAgentConfig:
    """Everything a node agent needs; JSON-encodable for the child process."""

    bin: str
    proc_root: str
    state_root: str
    volumes: str
    runtime_root: str
    storage_socket: str
    init_binary: str
    job_id: str
    node_id: str
    deployment_id: str
    node_control_token: str
    node_epoch: str
    port: int
    url: str | None
    admission_wait_seconds: float
    heartbeat_url: str
    heartbeat_token: str
    # Scenarios send heartbeats explicitly; the periodic one never fires in a test.
    heartbeat_interval_seconds: float = 3600.0
    # Node admission in a real noded (LocalFleet(rust_admission=True)): noded
    # owns the registry and this agent is a foreign writer, as in production.
    rust_admission: bool = False
    # noded as the node's front door (LocalFleet(front_door=True)): the agent
    # serves on this Unix socket behind it, with a foreign registry, and
    # noded runs creates, execs and the pause tier (vm_init's flags).
    unix_socket: str = ""
    rust_creates: bool = False
    rust_execs: bool = False
    rust_pause_tier: bool = False
    pause_tier: bool = False
    # 0 parks no idle sandbox.
    idle_park_seconds: float = 0.0
    # Environment images (LocalFleet(environments=True)): the registry, its
    # environments repository and trusted keys (key id -> raw key, hex), and
    # this node's environment backend socket and fake mount table.
    environment_registry_url: str = ""
    environment_repository: str = ""
    environment_trusted_keys: dict = field(default_factory=dict)
    environment_backend_socket: str = ""
    mount_table: str = ""


class FakeMountRunner:
    """Runs the image store's mount commands on this node's fake binaries.

    ``env -C dir VAR=value mount ...`` (a composed image's mount) keeps the
    real ``env``, so relative lowers resolve as they do in production.
    """

    commands = ("mount", "umount", "mountpoint")

    def __init__(self, bin_dir: Path) -> None:
        self.bin = bin_dir
        self.runner = SubprocessCommandRunner()

    def run(self, argv, *, timeout: float):
        argv = [str(item) for item in argv]
        index = 0
        if argv[0] == "env":
            index = next(position for position, item in enumerate(argv) if item in self.commands)
        if argv[index] not in self.commands:
            raise ValueError(f"the harness image store runs only {self.commands}: {argv}")
        argv[index] = str(self.bin / argv[index])
        return self.runner.run(argv, timeout=timeout)


def environment_rootfs_store(config: NodeAgentConfig, *, referenced) -> EnvironmentRootfsStore:
    """The production immutable image store, as ``build_direct_runtime_service``
    builds it, with the fake mount binaries, a fixed device pool and
    ``referenced(path)`` for the kernel's overlay dependencies: whether the
    fake mount table names ``path`` as a lower (``fake_mount.has_dependents``,
    which this module may not import)."""
    registry = EnvironmentArtifactRegistry(
        RegistryClient(config.environment_registry_url), config.environment_repository,
        {key: bytes.fromhex(value) for key, value in config.environment_trusted_keys.items()},
    )
    return EnvironmentRootfsStore(
        Path(config.state_root) / "image-cache", registry,
        EnvironmentBackendClient(Path(config.environment_backend_socket)),
        runner=FakeMountRunner(Path(config.bin)), referenced=referenced,
        block_devices=ENVIRONMENT_BLOCK_DEVICES,
    )


def assemble_node_agent(config: NodeAgentConfig, *, rootfs_store, fencer, sample_metrics):
    """Return ``(server, service)``, assembled as ``cmd_serve_direct_node_agent`` does."""
    bin_dir, state_root = Path(config.bin), Path(config.state_root)
    overlays = OverlayRootfsManager(
        rootfs_store,
        writable_root=Path(config.volumes),
        bundle_root=state_root / "bundles",
        runner=SubprocessCommandRunner(),
        mount_binary=str(bin_dir / "mount"),
        mountpoint_binary=str(bin_dir / "mountpoint"),
        umount_binary=str(bin_dir / "umount"),
        require_precreated_writable=True,
    )
    warden = DirectRunscWarden(
        DirectRunscWardenConfig(
            runsc=bin_dir / "runsc",
            runtime_root=Path(config.runtime_root),
            memory_root=Path(config.volumes),
            bundle_root=state_root / "bundles",
            journal_root=state_root / "journals",
            runtime_fingerprint=(ENVIRONMENT_FINGERPRINT if rootfs_store.backend_abi == HOST_EROFS_ABI
                                 else FINGERPRINT),
            proc_root=Path(config.proc_root),
            network="none",
            pause_tier=config.pause_tier,
            command_timeout_seconds=30.0,
            stop_timeout_seconds=10.0,
        ),
        storage=StorageNativeNodeClient(Path(config.storage_socket)),
        rootfs_lifecycle=overlays,
        fencer=fencer,
    )
    foreign = config.rust_admission or config.rust_creates
    provisioner = DirectSandboxProvisioner(
        registry=DirectSandboxRegistry(state_root / "direct-registry.sqlite", owner=not foreign,
                                       cached_reads=foreign),
        overlays=overlays,
        # The fake runtime never runs either init; the managed one only has to be
        # a trusted binary for managed_process sandboxes to be created.
        oci=DirectOciConfigBuilder(init_binary=Path(config.init_binary),
                                   managed_init_binary=Path(config.init_binary), network_mode="none"),
        warden=warden,
        disk_claim_policy=DiskClaimPolicy(),
    )
    service = DirectSandboxService(provisioner, max_concurrent_restores=4, max_concurrent_startups=4,
                                   idle_park_seconds=config.idle_park_seconds)
    service.admission_wait_seconds = config.admission_wait_seconds
    server = build_direct_node_agent_server(
        "127.0.0.1",
        config.port,
        service=service,
        image_file=state_root / "images.json",
        job_id=config.job_id,
        node_id=config.node_id,
        node_url=config.url,
        deployment_id=config.deployment_id,
        total_resources=ResourceQuantity(vcpu=8, memory_mb=16384, disk_mb=65536),
        image_runtime=DockerImageRuntime(dry_run=True),
        node_control_bearer_token=config.node_control_token,
        # Uncached, so a sample the test sets governs the very next admission.
        runtime_metrics_provider=SingleFlightRuntimeMetricsSampler(sample_metrics, freshness_seconds=0),
        node_epoch=config.node_epoch,
        heartbeat=HeartbeatSenderConfig(
            url=config.heartbeat_url,
            bearer_token=config.heartbeat_token,
            interval_seconds=config.heartbeat_interval_seconds,
        ),
        unix_socket=Path(config.unix_socket) if config.unix_socket else None,
        rust_creates=config.rust_creates,
        rust_execs=config.rust_execs,
        rust_pause_tier=config.rust_pause_tier,
    )
    if config.url is None:
        # The URL is part of route identity; restarts rebind this port.
        server.RequestHandlerClass.node_url = f"http://127.0.0.1:{server.server_address[1]}"
    return server, service
