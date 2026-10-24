import hashlib
import json
import shutil
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import patch
from urllib import error, request

from sandboxes.capabilities import HIBERNATE_LOCAL_CAPABILITY
from sandboxes.direct_oci import DirectOciConfigBuilder
from sandboxes.direct_provisioner import DirectSandboxProvisioner
from sandboxes.direct_registry import DirectRegistryError, DirectSandboxRegistry
from sandboxes.direct_service import DirectExecResult, DirectSandboxService
from sandboxes.direct_warden import DirectSandbox, DirectWardenError
from sandboxes.hibernation import (
    HibernationArtifactStore,
    HibernationRuntimeFingerprint,
    HibernationState,
)
from sandboxes.image_rootfs import (
    DockerImageConfig,
    MaterializedRootfs,
    OverlayRootfsLease,
    OverlayRootfsManager,
)
from sandboxes.images import DockerImageRuntime
from sandboxes.models import NodeRuntimeMetrics, ResourceQuantity, utc_now
from sandboxes.node_agent import (
    build_direct_node_agent_server as _build_direct_node_agent_server,
)
from sandboxes.node_runtime import DirectNodeRuntime
from sandboxes.pause_tier import PauseStats
from sandboxes.sandbox import (
    SandboxAdmissionClosedError,
    SandboxCapacityUnavailableError,
    SandboxConflictError,
    SandboxRestoreBusyError,
    SandboxOperation,
    SandboxSecuritySpec,
    SandboxSpec,
    sandbox_spec_fingerprint,
)
from sandboxes.storage_native_daemon import (
    StorageNativeCapacityError,
    StorageNativeConflictError,
    StorageNativeNodeError,
    StorageVolumeOwner,
    StorageVolumeRecord,
    StorageVolumeState,
)

TEST_TIER = "contract"


def build_direct_node_agent_server(*args, **kwargs):
    explicit_auth = "node_control_bearer_token" in kwargs
    kwargs.setdefault("node_control_bearer_token", "test-node-secret")
    kwargs.setdefault("deployment_id", "test-deployment")
    kwargs.setdefault("image_runtime", DockerImageRuntime(dry_run=True))
    server = _build_direct_node_agent_server(*args, **kwargs)
    if not explicit_auth:
        server.RequestHandlerClass._check_node_control_authorized = lambda _self: True
    return server


class FakeImageStore:
    def __init__(self, root: Path) -> None:
        rootfs = root / "image-rootfs"
        rootfs.mkdir()
        self.image = MaterializedRootfs(
            image_ref="image",
            image_id="sha256:" + "a" * 64,
            rootfs_identity_sha256="b" * 64,
            rootfs=rootfs,
            image_config=DockerImageConfig(command=("sleep", "3600")),
        )
        self.reconciled = False
        self.reconciled_roots: tuple[str, ...] = ()
        self.collected_image_ids: list[str] = []
        self.collection_reference_checks: list[bool] = []
        self.materialized_refs: list[str] = []
        self.fail_collect = False
        self.fail_reconcile = False

    def materialize(self, image_ref: str) -> MaterializedRootfs:
        if image_ref != "image":
            raise AssertionError("wrong image")
        self.materialized_refs.append(image_ref)
        return self.image

    @contextmanager
    def operation_lease(self, image_ref: str, environment_root=None):
        yield self.materialize(image_ref)

    def reconcile_images(self, referenced_image_ids, *, is_referenced) -> None:
        self.reconciled_roots = tuple(referenced_image_ids)
        del is_referenced
        if self.fail_reconcile:
            raise OSError("injected image reconciliation failure")
        self.reconciled = True

    def collect_image(self, image_id, *, is_referenced) -> bool:
        self.collected_image_ids.append(image_id)
        if self.fail_collect:
            raise OSError("injected image collection failure")
        referenced = is_referenced(image_id)
        self.collection_reference_checks.append(referenced)
        return not referenced

    def warm(self, image_ref: str) -> None:
        self.materialize(image_ref)

    def operation_snapshot(self) -> dict[str, int]:
        return {
            "active_operations": 0,
            "waiting_operations": 0,
            "max_concurrent_operations": 4,
        }


class FakeStorage:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.records: dict[tuple[str, int], StorageVolumeRecord] = {}
        self.next_project_id = 200_000
        self.delete_operation_ids: list[str] = []

    @property
    def active_records(self) -> dict[tuple[str, int], StorageVolumeRecord]:
        return {
            key: record
            for key, record in self.records.items()
            if record.state != StorageVolumeState.DELETED
        }

    def prepare_volume(
        self,
        owner: StorageVolumeOwner,
        *,
        operation_id: str,
        virtual_size: int,
    ) -> StorageVolumeRecord:
        key = (owner.sandbox_id, owner.sandbox_generation)
        existing = self.records.get(key)
        if existing is not None:
            if (
                existing.state == StorageVolumeState.DELETED
                or existing.owner != owner
                or existing.virtual_size != virtual_size
            ):
                raise StorageNativeConflictError(
                    "replayed storage preparation changed its owner"
                )
            return existing
        project_id = self.next_project_id
        self.next_project_id += 1
        record = self._record(
            owner,
            operation_id=operation_id,
            virtual_size=virtual_size,
            accounting_id=project_id,
        )
        self.records[key] = record
        return record

    def get_volume(self, volume_id: str) -> StorageVolumeRecord:
        for record in self.records.values():
            if record.volume_id == volume_id:
                return record
        raise StorageNativeConflictError("storage-native volume does not exist")

    def delete_volume(
        self,
        owner: StorageVolumeOwner,
        *,
        operation_id: str,
        expected_accounting_id: int | None = None,
        expected_virtual_size: int | None = None,
    ) -> StorageVolumeRecord:
        record = self.get_volume(owner.volume_id)
        if (
            record.owner != owner
            or (
                expected_accounting_id is not None
                and record.accounting_id != expected_accounting_id
            )
            or (
                expected_virtual_size is not None
                and record.virtual_size != expected_virtual_size
            )
        ):
            raise StorageNativeNodeError(
                "storage-native volume belongs to another owner"
            )
        if record.state == StorageVolumeState.DELETED:
            return record
        self.delete_operation_ids.append(operation_id)
        path = Path(record.mount_path)
        if path.exists():
            shutil.rmtree(path)
        deleted = replace(
            record,
            revision=record.revision + 1,
            state=StorageVolumeState.DELETED,
            operation_id=operation_id,
        )
        self.records[(owner.sandbox_id, owner.sandbox_generation)] = deleted
        return deleted

    def get_metrics(self) -> dict:
        return {}

    def _record(
        self,
        owner: StorageVolumeOwner,
        *,
        operation_id: str,
        virtual_size: int,
        accounting_id: int,
    ) -> StorageVolumeRecord:
        path = self.root / owner.volume_id
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        runtime = self.root.parent / "storage-runtime" / owner.volume_id
        return StorageVolumeRecord(
            volume_id=owner.volume_id,
            sandbox_id=owner.sandbox_id,
            sandbox_generation=owner.sandbox_generation,
            revision=1,
            state=StorageVolumeState.MOUNTED,
            operation_id=operation_id,
            virtual_size=virtual_size,
            runtime_dir=str(runtime / "runtime"),
            mount_path=str(path),
            source_image_config=str(runtime / "source.json"),
            device_owner_id="",
            accounting_id=accounting_id,
        )


class FakeOverlays(OverlayRootfsManager):
    require_precreated_writable = True

    def __init__(self, image_store: FakeImageStore, root: Path) -> None:
        self.image_store = image_store
        self.writable_root = root / "quota"
        self.bundle_root = root / "bundles"
        self.writable_root.mkdir()
        self.bundle_root.mkdir()

    def discard_unregistered(self, *, sandbox_id, sandbox_generation, workspace_directory=""):
        bundle = self.bundle_root / f"{sandbox_id}.sandbox-{sandbox_generation}"
        if bundle.exists():
            shutil.rmtree(bundle)

    def prepare(
        self,
        *,
        sandbox_id,
        sandbox_generation,
        image,
        config_template,
        spec_sha256,
        imported_parked=False,
        workspace_directory="",
        memory=None,
    ):
        incarnation = f"{sandbox_id}.sandbox-{sandbox_generation}"
        writable = self.writable_root / (workspace_directory or incarnation)
        if not writable.is_dir():
            raise AssertionError("quota was not prepared first")
        upper = writable / "upper"
        work = writable / "work"
        if not imported_parked:
            upper.mkdir()
        work.mkdir()
        bundle = self.bundle_root / incarnation
        merged = bundle / "rootfs"
        merged.mkdir(parents=True)
        (bundle / "config.json").write_text(
            json.dumps(config_template), encoding="utf-8"
        )
        sandbox = DirectSandbox(
            sandbox_id=sandbox_id,
            sandbox_generation=sandbox_generation,
            container_id=hashlib.sha256(
                f"{sandbox_id}:{sandbox_generation}".encode("utf-8")
            ).hexdigest(),
            spec_sha256=spec_sha256,
            rootfs_sha256=image.rootfs_identity_sha256,
            bundle=bundle,
            memory_directory=incarnation,
            workspace_directory=workspace_directory,
            memory=memory,
        )
        return OverlayRootfsLease(
            sandbox=sandbox,
            image=image,
            writable=writable,
            upper=upper,
            work=work,
            merged=merged,
            writable_owned_by_manager=False,
        )

    def release_sandbox(self, sandbox):
        if sandbox.bundle.exists():
            shutil.rmtree(sandbox.bundle)


class FakeWarden:
    memory_backing = None
    def application_memory_mode(self, sandbox_id, generation):
        return "ram"  # These admission fixtures model bounded unswappable heaps.
    def __init__(self, root: Path, storage: FakeStorage) -> None:
        fingerprint = HibernationRuntimeFingerprint(
            runsc_sha256="d" * 64,
            runsc_commit="e" * 40,
            platform="systrap",
            architecture="x86_64",
            page_size=4096,
            cpu_features_sha256="f" * 64,
            boot_config_sha256="1" * 64,
            rootfs_sha256="2" * 64,
        )
        quota = root / "quota"
        bundles = root / "bundles"
        self.config = SimpleNamespace(
            bundle_root=bundles,
            memory_root=quota,
            runtime_root=root / "runsc",
            network="none",
            runtime_fingerprint=fingerprint,
        )
        self.pause_stats = PauseStats()
        self.artifacts = HibernationArtifactStore(quota)
        self.records = {}
        self.storage = storage

    @staticmethod
    def key(sandbox):
        return sandbox.sandbox_id, sandbox.sandbox_generation

    def inspect(self, sandbox):
        return self.records.get(self.key(sandbox))

    def inspect_snapshot(self, sandbox):
        return self.records.get(self.key(sandbox))

    def reconcile_retired_memory_capacity(self):
        return 0

    def discard_unjournaled(self, sandbox):
        if self.inspect(sandbox) is not None:
            raise AssertionError("journal already exists")

    def create(self, sandbox, *, operation_id):
        del operation_id
        record = SimpleNamespace(state=HibernationState.RUNNING, hibernation_generation=1)
        self.records[self.key(sandbox)] = record
        return record

    def reconcile(self, sandbox):
        return self.records[self.key(sandbox)]

    def running_process_alive(self, sandbox):
        return self.records[self.key(sandbox)].state == HibernationState.RUNNING

    def running_snapshot(self, sandbox):
        record = self.records.get(self.key(sandbox))
        return record is not None and record.state == HibernationState.RUNNING

    def thaw(self, _sandbox):
        return None  # These fixtures never pause.

    def is_paused(self, _sandbox_id, _generation):
        return False

    @staticmethod
    def paused_keys():
        return []

    @staticmethod
    def workspace_record(_sandbox):
        return SimpleNamespace(state=StorageVolumeState.MOUNTED)

    def storage_records_snapshot(self, sandboxes):
        return {
            sandbox.memory_directory: self.storage.get_volume(sandbox.memory_directory)
            for sandbox in sandboxes
        }

    def park(self, sandbox, *, operation_id):
        del operation_id
        record = SimpleNamespace(state=HibernationState.PARKED, hibernation_generation=1)
        self.records[self.key(sandbox)] = record
        return record

    def prepare_restore_memory(self, sandbox):
        if self.records[self.key(sandbox)].state != HibernationState.PARKED:
            raise AssertionError("memory placement requires a parked owner")

    def resume(self, sandbox, *, operation_id, timings=None, before_restore=None):
        del operation_id
        if before_restore is not None:
            before_restore()
        if timings is not None:
            timings["runsc_restore"] = 1.0
        record = SimpleNamespace(state=HibernationState.RUNNING, hibernation_generation=1)
        self.records[self.key(sandbox)] = record
        return record

    def adopt_parked(self, sandbox, manifest):
        del manifest
        record = SimpleNamespace(state=HibernationState.PARKED, hibernation_generation=1)
        self.records[self.key(sandbox)] = record
        return record

    @contextmanager
    def exec_lease(self, sandbox, argv, *, env=None, working_dir=None, user=None,
                   keep_paused=False):
        del env, working_dir, user, keep_paused
        yield ("runsc", "exec", sandbox.container_id, *argv)

    def delete(self, sandbox):
        self.records.pop(self.key(sandbox), None)


class FakeProcessRunner:
    def __init__(self) -> None:
        self.calls = []

    def run(
        self,
        argv,
        *,
        input_bytes,
        timeout_seconds,
        max_stdout_bytes,
        max_stderr_bytes,
    ):
        del timeout_seconds, max_stdout_bytes, max_stderr_bytes
        self.calls.append((tuple(argv), input_bytes))
        return DirectExecResult(tuple(argv), 0, b"ok\n", b"")


class DirectProvisionerTests(unittest.TestCase):
    def make(self, root: Path, images: FakeImageStore | None = None):
        images = images or FakeImageStore(root)
        overlays = FakeOverlays(images, root)
        storage = FakeStorage(overlays.writable_root)
        warden = FakeWarden(root, storage)
        warden.rootfs_lifecycle = overlays
        registry = DirectSandboxRegistry((root / "registry.sqlite").resolve())
        provisioner = DirectSandboxProvisioner(
            registry=registry,
            overlays=overlays,
            oci=DirectOciConfigBuilder(),
            warden=warden,
        )
        return provisioner, registry, storage, images, warden

    @staticmethod
    def spec() -> SandboxSpec:
        return SandboxSpec(
            id="sandbox",
            image="image",
            memory_mb=1024,
            disk_mb=2048,
            network="none",
            security=SandboxSecuritySpec(init=False),
        )

    @staticmethod
    def create(
        service: DirectSandboxService,
        spec: SandboxSpec,
        *,
        generation: int = 7,
    ):
        return service.create(
            spec,
            operation=SandboxOperation(
                operation_id=f"create:{spec.id}:{generation}",
                generation=generation,
                kind="create",
                spec_hash=sandbox_spec_fingerprint(spec),
            ),
        )

    def test_storage_capacity_rejection_is_retryable_and_rolls_back_owner(
        self,
    ) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            provisioner, registry, storage, _, _ = self.make(root)
            service = DirectSandboxService(
                provisioner,
                process_runner=FakeProcessRunner(),
            )
            with patch.object(
                storage,
                "prepare_volume",
                side_effect=StorageNativeCapacityError(
                    "storage-native ublk device capacity is exhausted"
                ),
            ):
                with self.assertRaisesRegex(
                    SandboxCapacityUnavailableError,
                    "ublk device capacity is exhausted",
                ):
                    self.create(service, self.spec())

            self.assertIsNone(registry.get("sandbox"))
            self.assertEqual(storage.active_records, {})

    @contextmanager
    def running_server(self, root: Path, service: DirectSandboxService):
        server = build_direct_node_agent_server(
            "127.0.0.1",
            0,
            service=service,
            image_file=root / "images.json",
            job_id="job",
            node_id="node",
        )
        thread = Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        thread.start()
        try:
            host, port = server.server_address
            yield f"http://{host}:{port}"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1)

    def test_create_rejects_noncanonical_storage_record(self) -> None:
        cases = (
            (
                "owner",
                lambda record, _root: replace(record, sandbox_id="other"),
                "another quota owner",
            ),
            (
                "path",
                lambda record, root: replace(
                    record,
                    mount_path=str(root / "unexpected"),
                ),
                "unexpected mount path",
            ),
            (
                "size",
                lambda record, _root: replace(
                    record,
                    virtual_size=record.virtual_size + 1,
                ),
                "another quota owner",
            ),
            (
                "accounting",
                lambda record, _root: replace(record, accounting_id=0),
                "invalid accounting ID",
            ),
        )
        for label, mutate, message in cases:
            with self.subTest(label=label), TemporaryDirectory() as raw:
                root = Path(raw).resolve()
                provisioner, _, storage, _, _ = self.make(root)
                prepare = storage.prepare_volume

                def invalid(owner, **kwargs):
                    return mutate(prepare(owner, **kwargs), root)

                storage.prepare_volume = invalid
                with self.assertRaisesRegex(StorageNativeNodeError, message):
                    provisioner.create(
                        spec=self.spec(),
                        sandbox_generation=7,
                        operation_id="create:7",
                    )

    def test_an_idle_image_store_keeps_a_deleted_sandboxs_image(self) -> None:
        # The environment store keeps the composition mounted for the image's
        # next sandbox; its own budget sweep, run by the reconciler, collects it.
        with TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            images = FakeImageStore(root)
            images.retains_idle_images, used, sweeps = True, [], []
            images.note_used = used.append
            images.collect_idle = lambda: sweeps.append(images.is_referenced(images.image.image_id)) or ()
            provisioner, _, _, _, _ = self.make(root, images)
            self.assertEqual(images.is_referenced, provisioner.image_referenced)
            created = provisioner.create(spec=self.spec(), sandbox_generation=1, operation_id="create:1")
            provisioner.note_image_used(None)  # A rollback before any rootfs.
            provisioner.note_image_used(images.image.image_id)  # noded's finish.
            provisioner.delete(created.sandbox_id)
            self.assertEqual(images.collected_image_ids, [])
            self.assertEqual(used, [images.image.image_id] * 2)
            self.assertEqual(provisioner.evict_images_if_needed(), ())
            self.assertEqual(sweeps, [False])

    def test_post_commit_image_collection_failure_is_deferred(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            provisioner, registry, _, images, _ = self.make(root)
            provisioner.start()
            created = provisioner.create(
                spec=self.spec(),
                sandbox_generation=7,
                operation_id="create:7",
            )
            images.fail_collect = True

            provisioner.delete(created.sandbox_id)

            self.assertIsNone(registry.get(created.sandbox_id))
            self.assertTrue(provisioner.image_cache_reconciliation_pending)
            images.fail_collect = False
            self.assertTrue(provisioner.reconcile_image_cache_if_pending())
            self.assertFalse(provisioner.image_cache_reconciliation_pending)

    def test_restart_never_turns_interrupted_import_into_new_sandbox(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            provisioner, registry, quota, _, warden = self.make(root)
            planned = registry.plan_import(
                spec=self.spec(),
                sandbox_generation=9,
                operation_id="create:9",
                runtime_compatibility_sha256=(provisioner.runtime_compatibility_sha256),
                migration_id="move:interrupted",
                migration_sha256="a" * 64,
            )
            total_mb = provisioner._quota_total_mb(planned)
            record = quota.prepare_volume(
                provisioner._storage_owner(planned),
                operation_id=planned.operation_id,
                virtual_size=total_mb * 1024 * 1024,
            )
            registry.commit_import_quota(
                "sandbox",
                expected_revision=planned.revision,
                project_id=record.accounting_id,
                total_mb=total_mb,
                quota_path=Path(record.mount_path),
            )

            results = provisioner.start()

            self.assertEqual(results, ())
            self.assertEqual(registry.get("sandbox").phase, "importing")
            self.assertNotIn(("sandbox", 9), warden.records)

    def test_delete_activated_import_preserves_migration_storage_fence(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            provisioner, registry, quota, _, warden = self.make(root)
            migration_id = "move:activated"
            migration_sha256 = "a" * 64
            planned = registry.plan_import(
                spec=self.spec(),
                sandbox_generation=9,
                operation_id="create:9",
                runtime_compatibility_sha256=(provisioner.runtime_compatibility_sha256),
                migration_id=migration_id,
                migration_sha256=migration_sha256,
            )
            total_mb = provisioner._quota_total_mb(planned)
            volume = quota.prepare_volume(
                provisioner._storage_owner(planned),
                operation_id=planned.operation_id,
                virtual_size=total_mb * 1024 * 1024,
            )
            importing = registry.commit_import_quota(
                planned.sandbox_id,
                expected_revision=planned.revision,
                project_id=volume.accounting_id,
                total_mb=total_mb,
                quota_path=Path(volume.mount_path),
            )
            bundle = (root / "bundles" / "sandbox.sandbox-9").resolve()
            bundle.mkdir()
            sandbox = DirectSandbox(
                sandbox_id=planned.sandbox_id,
                sandbox_generation=planned.sandbox_generation,
                container_id="c" * 64,
                spec_sha256=planned.spec_sha256,
                rootfs_sha256="d" * 64,
                bundle=bundle,
                memory_directory="sandbox.sandbox-9",
            )
            rootfs = registry.commit_import_rootfs(
                planned.sandbox_id,
                expected_revision=importing.revision,
                image_id="sha256:" + "e" * 64,
                sandbox=sandbox,
            )
            ready = registry.commit_import_ready(
                planned.sandbox_id,
                expected_revision=rootfs.revision,
                migration_id=migration_id,
                migration_sha256=migration_sha256,
            )
            owned = registry.activate_import(
                planned.sandbox_id,
                expected_revision=ready.revision,
                migration_id=migration_id,
                migration_sha256=migration_sha256,
            )
            warden.records[(owned.sandbox_id, owned.sandbox_generation)] = (
                SimpleNamespace(state=HibernationState.PARKED)
            )

            provisioner.delete(owned.sandbox_id)

            self.assertIsNone(registry.get(owned.sandbox_id))
            self.assertEqual(quota.active_records, {})
            self.assertEqual(
                quota.delete_operation_ids,
                [f"quota-delete:{volume.accounting_id}:{migration_id}"],
            )

    def test_delete_fails_closed_when_storage_authority_is_absent(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            provisioner, registry, quota, _, _ = self.make(root)
            created = provisioner.create(
                spec=self.spec(),
                sandbox_generation=7,
                operation_id="create:7",
            )
            identity = (
                created.sandbox_id,
                created.sandbox_generation,
            )
            quota.records.pop(identity)

            with self.assertRaisesRegex(RuntimeError, "quota owner is absent"):
                provisioner.delete(created.sandbox_id)

            deleting = registry.get(created.sandbox_id)
            assert deleting is not None
            self.assertEqual(deleting.phase, "deleting")

    def test_start_serves_while_failed_warden_delete_retries(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            provisioner, registry, quota, _, warden = self.make(root)
            created = provisioner.create(
                spec=self.spec(),
                sandbox_generation=7,
                operation_id="create:7",
            )
            registry.begin_delete(
                created.sandbox_id,
                expected_revision=created.revision,
            )
            original_delete = warden.delete
            failures_remaining = 1

            def transient_warden_delete(sandbox):
                nonlocal failures_remaining
                if failures_remaining:
                    failures_remaining -= 1
                    raise OSError("injected mounted-volume cleanup failure")
                return original_delete(sandbox)

            warden.delete = transient_warden_delete
            service = DirectSandboxService(
                provisioner,
                process_runner=FakeProcessRunner(),
                deletion_reconcile_interval_seconds=0.01,
            )
            service.start()
            try:
                sandbox_id = created.sandbox_id
                self.assertEqual(registry.get(sandbox_id).phase, "deleting")
                deadline = monotonic() + 2
                while registry.get(sandbox_id) is not None and monotonic() < deadline:
                    sleep(0.01)

                self.assertIsNone(registry.get(sandbox_id))
                self.assertEqual(quota.active_records, {})
            finally:
                service.stop()

    def test_wake_refreshes_idle_interval_and_stale_timer_cannot_repark(self) -> None:
        with TemporaryDirectory() as raw:
            provisioner, _, _, _, warden = self.make(Path(raw).resolve())
            service = DirectSandboxService(
                provisioner, process_runner=FakeProcessRunner(), idle_park_seconds=1
            )
            created = self.create(service, self.spec())
            key = (created.spec.id, created.generation)
            service.park(created.spec.id, operation_id="park:before-wake")
            service._last_activity[key] = monotonic() - 60
            service.wake(
                created.spec.id, generation=created.generation,
                operation_id="wake:idle-test",
            )
            self.assertLess(service.idle_for_seconds(*key), 1)
            with patch.object(warden, "park", wraps=warden.park) as park:
                record = service.park(
                    created.spec.id, operation_id="park:stale-timer", background=True
                )
                self.assertEqual(record.state, "running")
                park.assert_not_called()

            # An idempotent wake is activity too; an explicit park continues
            # to work even within that refreshed timer interval.
            service._last_activity[key] = monotonic() - 60
            service.wake(
                created.spec.id, generation=created.generation,
                operation_id="wake:already-running",
            )
            self.assertLess(service.idle_for_seconds(*key), 1)
            record = service.park(created.spec.id, operation_id="park:explicit")
            self.assertEqual(record.state, "parked")

    def test_file_wait_releases_owner_lock_and_fences_replacement_and_drain(self) -> None:
        for change in ("generation", "drain"):
            with self.subTest(change=change), TemporaryDirectory() as raw:
                provisioner, registry, _, _, _ = self.make(Path(raw).resolve())
                runner = FakeProcessRunner()
                service = DirectSandboxService(provisioner, process_runner=runner)
                record = self.create(service, self.spec())
                original_get = registry.get
                replaced = []

                def wait(_timeout):
                    # Match Condition.wait: release its lock during the wait.
                    service._capacity_guard.release()
                    try:
                        with service._try_lock(record.spec.id, record.generation) as acquired:
                            self.assertTrue(acquired)
                        self.assertEqual(service.activity_snapshot().active_operations, 0)
                        if change == "drain":
                            service.close_admission()
                        else:
                            replaced.append(True)
                    finally:
                        service._capacity_guard.acquire()

                def get(sandbox_id):
                    result = original_get(sandbox_id)
                    return replace(result, sandbox_generation=result.sandbox_generation + 1) if replaced else result

                expected = SandboxConflictError if change == "generation" else SandboxAdmissionClosedError
                with (
                    patch.object(service, "acquire_exec_capacity", side_effect=SandboxCapacityUnavailableError("memory pressure")),
                    patch.object(service._admission_changed, "wait", side_effect=wait),
                    patch.object(registry, "get", side_effect=get),
                    self.assertRaises(expected),
                ):
                    service.write_file(record.spec.id, "/marker", b"payload")
                self.assertEqual(runner.calls, [])

    def test_wake_that_loses_a_storage_fence_stays_parked_and_retryable(self):
        from sandboxes.storage_native_daemon import (
            StorageNativeCapacityError, StorageNativeConflictError,
        )
        with TemporaryDirectory() as raw:
            provisioner, _, _, _, _ = self.make(Path(raw).resolve())
            service = DirectSandboxService(provisioner)
            record = self.create(service, self.spec())
            service.park(record.spec.id, operation_id="park:fence")
            stale = StorageNativeConflictError("stale storage revision 8; current revision is 9")
            with patch.object(service.warden, "resume", side_effect=stale):
                with self.assertRaises(SandboxRestoreBusyError):
                    service.wake(record.spec.id, generation=record.generation, operation_id="wake:fence")
            self.assertEqual(service.get(record.spec.id).state, "parked")
            # Capacity keeps its own translation.
            with patch.object(service.warden, "resume", side_effect=StorageNativeCapacityError("full")):
                with self.assertRaises(SandboxCapacityUnavailableError):
                    service.wake(record.spec.id, generation=record.generation, operation_id="wake:capacity")
            self.assertEqual(service.activity_snapshot().active_operations, 0)

    def test_file_http_pressure_deadline_is_safe_to_retry(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            provisioner, _, _, _, _ = self.make(root)
            runner = FakeProcessRunner()
            service = DirectSandboxService(provisioner, process_runner=runner)
            record = self.create(service, self.spec())
            service.admission_wait_seconds = 0.02
            server = build_direct_node_agent_server(
                "127.0.0.1", 0, service=service, image_file=root / "images.json",
                job_id="job", node_id="node", total_resources=ResourceQuantity(vcpu=4, memory_mb=8192),
                runtime_metrics_provider=lambda: NodeRuntimeMetrics(
                    collected_at=utc_now(), cpu_percent=10, cpu_count=4,
                    memory_total_mb=8192, memory_available_mb=1024,
                    memory_psi_full_avg10=20,
                ),
            )
            thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            try:
                for method in ("PUT", "GET"):
                    with self.subTest(method=method):
                        req = request.Request(
                            f"http://127.0.0.1:{server.server_port}/v1/sandboxes/{record.spec.id}/files?path=/marker",
                            method=method, data=b"payload" if method == "PUT" else None,
                        )
                        with self.assertRaises(error.HTTPError) as rejected:
                            request.urlopen(req, timeout=5)
                        with rejected.exception as response:
                            self.assertEqual(response.code, 503)
                            body = json.load(response)
                            self.assertEqual(body["error_code"], "node_active_exec_deferred")
                            self.assertTrue(body["retryable"])
                            self.assertNotIn("lifecycle_state", body)
                            self.assertEqual(response.headers["X-UCloud-Sandbox-Retryable"], "true")
                self.assertEqual(runner.calls, [])
                self.assertEqual(service.activity_snapshot().active_operations, 0)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_evict_published_requires_exact_parked_registry_authority(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            provisioner, registry, quota, _, warden = self.make(root)
            service = DirectSandboxService(
                provisioner,
                process_runner=FakeProcessRunner(),
            )
            created = self.create(service, self.spec())
            service.park(created.spec.id, operation_id="park:test")
            digest = "sha256:" + "a" * 64
            warden.workspace_record = lambda _sandbox: SimpleNamespace(
                state=StorageVolumeState.PUBLISHED,
                published_manifest_digest=digest,
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "does not match the durable route",
            ):
                service.evict_published(
                    created.spec.id,
                    generation=created.generation,
                    snapshot_manifest_digest="sha256:" + "b" * 64,
                )

            self.assertIsNotNone(registry.get(created.spec.id))
            service.evict_published(
                created.spec.id,
                generation=created.generation,
                snapshot_manifest_digest=digest,
            )
            service.evict_published(
                created.spec.id,
                generation=created.generation,
                snapshot_manifest_digest=digest,
            )

            self.assertIsNone(registry.get(created.spec.id))
            self.assertEqual(quota.active_records, {})

    def test_publish_parked_requires_exact_owned_incarnation(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            provisioner, _, _, _, _ = self.make(root)
            service = DirectSandboxService(
                provisioner,
                process_runner=FakeProcessRunner(),
            )
            created = self.create(service, self.spec())
            sentinel = object()

            with patch.object(
                service,
                "_storage_native_snapshot_locked",
                return_value=sentinel,
            ) as publish:
                observed = service.publish_parked(
                    created.spec.id,
                    generation=created.generation,
                    create_operation_id=created.operation_id,
                    spec_hash=created.spec_hash,
                )
                with self.assertRaisesRegex(RuntimeError, "does not own"):
                    service.publish_parked(
                        created.spec.id,
                        generation=created.generation + 1,
                        create_operation_id=created.operation_id,
                        spec_hash=created.spec_hash,
                    )

            self.assertIs(observed, sentinel)
            publish.assert_called_once()

    def test_start_preserves_owned_quarantine_and_serves_healthy_sandbox(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            provisioner, registry, storage, _, warden = self.make(root)
            runner = FakeProcessRunner()
            service = DirectSandboxService(provisioner, process_runner=runner)
            healthy = self.create(service, replace(self.spec(), id="healthy"))
            bad = self.create(service, replace(self.spec(), id="quarantined"))
            quarantine = SimpleNamespace(state=HibernationState.RECOVERY_REQUIRED)
            warden.records[(bad.spec.id, bad.generation)] = quarantine
            registrations = registry.snapshot().records
            storage_records = dict(storage.active_records)

            try:
                with (
                    patch.object(warden, "create", wraps=warden.create) as create,
                    patch.object(warden, "delete", wraps=warden.delete) as delete,
                    patch.object(
                        warden, "reconcile", wraps=warden.reconcile
                    ) as reconcile,
                    patch.object(warden, "resume", wraps=warden.resume) as resume,
                ):
                    for _ in range(2):
                        records = service.start()
                        expected = {
                            healthy.spec.id: HibernationState.RUNNING.value,
                            bad.spec.id: HibernationState.RECOVERY_REQUIRED.value,
                        }
                        self.assertEqual(
                            {r.spec.id: r.state for r in records}, expected
                        )
                        self.assertEqual(
                            {r.spec.id: r.state for r in service.list()}, expected
                        )
                        self.assertEqual(
                            service.get(bad.spec.id).state, expected[bad.spec.id]
                        )
                    reconcile.assert_not_called()
                    with self.assertRaisesRegex(DirectWardenError, "cannot wake"):
                        service.wake(
                            bad.spec.id,
                            generation=bad.generation,
                            operation_id="wake:quarantined:7",
                        )
                    with self.assertRaisesRegex(
                        DirectWardenError, "cannot accept traffic"
                    ):
                        service.exec(bad.spec.id, ("true",))
                    self.assertEqual(runner.calls, [])
                    self.assertEqual(
                        service.exec(healthy.spec.id, ("true",)).exit_code, 0
                    )
                    create.assert_not_called()
                    delete.assert_not_called()
                    resume.assert_not_called()
                self.assertIs(warden.records[(bad.spec.id, bad.generation)], quarantine)
                self.assertEqual(registry.snapshot().records, registrations)
                self.assertEqual(storage.active_records, storage_records)
            finally:
                service.stop()

    def test_start_accepts_owned_transition_reconciled_to_quarantine(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            provisioner, registry, storage, _, warden = self.make(root)
            service = DirectSandboxService(
                provisioner, process_runner=FakeProcessRunner()
            )
            bad = self.create(service, self.spec())
            warden.records[
                (bad.spec.id, bad.generation)
            ].state = HibernationState.HIBERNATING
            registration = registry.get(bad.spec.id)
            storage_records = dict(storage.active_records)

            def quarantine(sandbox):
                record = SimpleNamespace(state=HibernationState.RECOVERY_REQUIRED)
                warden.records[warden.key(sandbox)] = record
                return record

            with patch.object(warden, "reconcile", side_effect=quarantine) as reconcile:
                self.assertEqual(provisioner.start(), (registration,))
                self.assertEqual(provisioner.start(), (registration,))
                reconcile.assert_called_once()
            self.assertEqual(storage.active_records, storage_records)

    def test_memory_retry_rechecks_current_shape_and_owner_generation(self) -> None:
        for action in ("shape", "wake"):
            with self.subTest(action=action), TemporaryDirectory() as raw:
                provisioner, registry, _, _, warden = self.make(Path(raw).resolve())
                service = DirectSandboxService(provisioner)
                record = self.create(service, self.spec())
                if action == "wake":
                    service.park(record.spec.id, operation_id="park:generation")
                original_get = registry.get
                calls = [0]

                def get(sandbox_id):
                    result = original_get(sandbox_id)
                    if calls[0] > 1 and action != "shape":
                        return replace(result, sandbox_generation=record.generation + 1)
                    return result

                def sample():
                    calls[0] += 1
                    if calls[0] == 2 and action == "shape":
                        service.configure_active_capacity(
                            ResourceQuantity(vcpu=0.5, memory_mb=8192),
                            runtime_metrics_provider=sample,
                        )
                    return NodeRuntimeMetrics(
                        collected_at=utc_now(),
                        cpu_percent=95.0 if calls[0] == 1 else 10.0,
                        cpu_count=4,
                        memory_total_mb=8192,
                        memory_available_mb=2048 if calls[0] == 1 else 8192,
                    )

                service.configure_active_capacity(
                    ResourceQuantity(vcpu=4, memory_mb=8192),
                    runtime_metrics_provider=sample,
                )
                with (
                    patch.object(registry, "get", side_effect=get),
                    patch.object(warden, "resume", wraps=warden.resume) as resume,
                ):
                    if action == "shape":
                        with self.assertRaisesRegex(
                            SandboxCapacityUnavailableError, "physical node shape"
                        ):
                            with service._reserve_active_capacity(
                                "new", 1, ResourceQuantity(vcpu=1, memory_mb=1024)
                            ):
                                self.fail("shrunk capacity admitted")
                    elif action == "wake":
                        with self.assertRaisesRegex(DirectWardenError, "generation"):
                            service.wake(
                                record.spec.id,
                                generation=record.generation,
                                operation_id="wake:generation",
                            )
                    else:
                        with self.assertRaisesRegex(SandboxConflictError, "generation"):
                            service.acquire_exec_capacity(
                                record.spec.id, record.generation
                            )
                    resume.assert_not_called()
                self.assertEqual(service.activity_snapshot().active_operations, 0)

    def test_exec_checks_drain_and_owner_after_sampling_without_cpu_wait(self) -> None:
        for changed in ("drain", "owner"):
            with self.subTest(changed=changed), TemporaryDirectory() as raw:
                provisioner, registry, _, _, _ = self.make(Path(raw).resolve())
                service = DirectSandboxService(provisioner)
                record = self.create(service, self.spec())
                original_get = registry.get
                sampled = []

                def get(sandbox_id):
                    result = original_get(sandbox_id)
                    if sampled and changed == "owner":
                        return replace(result, sandbox_generation=record.generation + 1)
                    return result

                def sample():
                    sampled.append(True)
                    if changed == "drain":
                        service.close_admission()
                    return NodeRuntimeMetrics(
                        collected_at=utc_now(), cpu_percent=100.0,
                        cpu_count=4, memory_total_mb=8192, memory_available_mb=8192,
                    )

                service.configure_active_capacity(
                    ResourceQuantity(vcpu=4, memory_mb=8192),
                    runtime_metrics_provider=sample,
                )
                expected = (
                    SandboxAdmissionClosedError if changed == "drain"
                    else SandboxConflictError
                )
                with patch.object(registry, "get", side_effect=get):
                    with self.assertRaises(expected):
                        service.acquire_exec_capacity(record.spec.id, record.generation)
                self.assertEqual(service.activity_snapshot().active_operations, 0)

    def test_abort_move_recovers_digest_after_lost_prepare_response(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            provisioner, registry, _, _, _ = self.make(root)
            service = DirectSandboxService(
                provisioner,
                process_runner=FakeProcessRunner(),
            )
            created = self.create(service, self.spec())
            registration = registry.get(created.spec.id)
            assert registration is not None
            moving = registry.begin_move_out(
                created.spec.id,
                expected_revision=registration.revision,
                migration_id="wake:lost-prepare-response",
                migration_sha256="d" * 64,
            )

            restored = service.abort_move(
                created.spec.id,
                migration_id=moving.migration_id,
                migration_sha256="",
            )

            self.assertEqual(restored.spec.id, created.spec.id)
            durable = registry.get(created.spec.id)
            assert durable is not None
            self.assertEqual(durable.phase, "owned")
            self.assertEqual(durable.migration_id, "")
            self.assertEqual(durable.migration_sha256, "")

    def test_abort_move_without_digest_still_requires_exact_migration_id(
        self,
    ) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            provisioner, registry, _, _, _ = self.make(root)
            service = DirectSandboxService(
                provisioner,
                process_runner=FakeProcessRunner(),
            )
            created = self.create(service, self.spec())
            registration = registry.get(created.spec.id)
            assert registration is not None
            registry.begin_move_out(
                created.spec.id,
                expected_revision=registration.revision,
                migration_id="wake:owned-by-worker",
                migration_sha256="e" * 64,
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "source does not own this prepared migration",
            ):
                service.abort_move(
                    created.spec.id,
                    migration_id="wake:different-migration",
                    migration_sha256="",
                )

            durable = registry.get(created.spec.id)
            assert durable is not None
            self.assertEqual(durable.phase, "moving_out")
            self.assertEqual(durable.migration_id, "wake:owned-by-worker")

    def test_expired_migration_preserves_ownership_and_inventory(self) -> None:
        with TemporaryDirectory() as raw:
            provisioner, registry, _, _, _ = self.make(Path(raw).resolve())
            service = DirectSandboxService(provisioner, process_runner=FakeProcessRunner())
            created = self.create(service, self.spec())
            registration = registry.get(created.spec.id)
            assert registration is not None
            moving = registry.begin_move_out(
                created.spec.id,
                expected_revision=registration.revision,
                migration_id="wake:expiry-race",
                migration_sha256="e" * 64,
            )
            manager = DirectNodeRuntime(service)
            with patch("sandboxes.sandbox.SandboxRecord.is_expired", return_value=True):
                records = manager.list()
            self.assertEqual([r.spec.id for r in records], [created.spec.id])
            self.assertEqual(registry.get(created.spec.id), moving)
            self.assertEqual(service._locks, {})
            # Once ownership returns to this node, normal expiry can delete it.
            service.abort_move(
                created.spec.id, migration_id=moving.migration_id,
                migration_sha256=moving.migration_sha256,
            )
            self.assertTrue(service.try_delete(created.spec.id, generation=created.generation))
            self.assertIsNone(registry.get(created.spec.id))

    def test_try_delete_retires_absent_mismatched_and_busy_lock_entries(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            provisioner, _, _, _, _ = self.make(root)
            service = DirectSandboxService(
                provisioner,
                process_runner=FakeProcessRunner(),
            )
            created = self.create(service, self.spec())

            self.assertTrue(service.try_delete("absent", generation=1))
            self.assertFalse(
                service.try_delete(created.spec.id, generation=created.generation + 1)
            )
            self.assertEqual(service._locks, {})

            acquired = Event()
            release = Event()

            def hold_lifecycle_lock() -> None:
                with service._lock(created.spec.id, created.generation):
                    acquired.set()
                    if not release.wait(timeout=5):
                        raise AssertionError("test did not release lifecycle lock")

            holder = Thread(target=hold_lifecycle_lock)
            holder.start()
            self.assertTrue(acquired.wait(timeout=2))
            self.assertFalse(
                service.try_delete(created.spec.id, generation=created.generation)
            )
            with self.assertRaisesRegex(SandboxCapacityUnavailableError, "lifecycle is busy"):
                with service._request_lock(created.spec.id, created.generation):
                    pass
            with self.assertRaisesRegex(SandboxCapacityUnavailableError, "lifecycle is busy"):
                with service._request_lock(
                    created.spec.id, created.generation, wait_seconds=0.05,
                ):
                    pass
            queued = Event()

            def queue_for_lifecycle_lock() -> None:
                with service._request_lock(
                    created.spec.id, created.generation, wait_seconds=5,
                ):
                    queued.set()

            waiter = Thread(target=queue_for_lifecycle_lock)
            waiter.start()
            self.assertFalse(queued.wait(timeout=0.1))
            release.set()
            holder.join(timeout=5)
            waiter.join(timeout=5)
            self.assertTrue(queued.is_set())

            self.assertFalse(holder.is_alive())
            self.assertEqual(service._locks, {})

    def test_direct_node_park_and_explicit_wake_endpoints(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            provisioner, _, _, _, _ = self.make(root)
            service = DirectSandboxService(
                provisioner,
                process_runner=FakeProcessRunner(),
            )
            created = self.create(service, self.spec())
            with self.running_server(root, service) as base:
                with request.urlopen(f"{base}/v1/heartbeat") as response:
                    before = json.load(response)["heartbeat"]
                self.assertIn(
                    HIBERNATE_LOCAL_CAPABILITY,
                    before["capabilities"],
                )
                self.assertNotIn("hibernate-local-v1", before["capabilities"])
                park_request = request.Request(
                    f"{base}/v1/sandboxes/{created.spec.id}/park",
                    data=json.dumps({"operation_id": "park:test"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with request.urlopen(park_request) as response:
                    parked_payload = json.load(response)
                parked = parked_payload["sandbox"]
                self.assertEqual(parked["state"], HibernationState.PARKED.value)
                self.assertEqual(
                    parked_payload["node_epoch"],
                    before["node_epoch"],
                )
                self.assertGreater(
                    parked_payload["activity_epoch"],
                    before["activity_epoch"],
                )
                with self.assertRaises(RuntimeError):
                    service.wake(
                        created.spec.id,
                        generation=created.generation + 1,
                        operation_id="wake:stale",
                    )

                wake_request = request.Request(
                    f"{base}/v1/sandboxes/{created.spec.id}/wake",
                    data=json.dumps(
                        {
                            "generation": created.generation,
                            "operation_id": "wake:test",
                        }
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with request.urlopen(wake_request) as response:
                    woken_payload = json.load(response)
                woken = woken_payload["sandbox"]
                self.assertEqual(woken["state"], HibernationState.RUNNING.value)
                self.assertEqual(
                    woken_payload["node_epoch"],
                    parked_payload["node_epoch"],
                )
                self.assertGreater(
                    woken_payload["activity_epoch"],
                    parked_payload["activity_epoch"],
                )
                self.assertEqual(
                    service.get(created.spec.id).state,
                    HibernationState.RUNNING.value,
                )

    def test_wake_route_answers_restore_saturation_with_its_retry_contract(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            provisioner, _, _, _, warden = self.make(root)
            service = DirectSandboxService(
                provisioner, process_runner=FakeProcessRunner(), max_concurrent_restores=1,
            )
            service.admission_wait_seconds = 0.05
            for sandbox_id in ("sandbox", "busy"):
                self.create(service, replace(self.spec(), id=sandbox_id))
                service.park(sandbox_id, operation_id=f"park:{sandbox_id}")
            resume, entered, release = warden.resume, Event(), Event()

            def held_resume(*args, **kwargs):
                entered.set()
                self.assertTrue(release.wait(5))
                return resume(*args, **kwargs)

            def wake(base):
                return request.Request(
                    f"{base}/v1/sandboxes/sandbox/wake",
                    data=json.dumps({"generation": 7, "operation_id": "wake:test"}).encode(),
                    headers={"Content-Type": "application/json"}, method="POST",
                )

            with self.running_server(root, service) as base:
                # A wake of another sandbox holds the node's only restore slot.
                with patch.object(warden, "resume", side_effect=held_resume), \
                        ThreadPoolExecutor(max_workers=1) as pool:
                    busy = pool.submit(service.wake, "busy", generation=7, operation_id="wake:busy")
                    try:
                        self.assertTrue(entered.wait(5))
                        with self.assertRaises(error.HTTPError) as rejected:
                            request.urlopen(wake(base), timeout=5)
                    finally:
                        release.set()
                    self.assertEqual(busy.result(5).state, HibernationState.RUNNING.value)
                with rejected.exception as response:
                    self.assertEqual(response.code, 503)
                    self.assertEqual(response.headers["Retry-After"], "1")
                    body = json.load(response)
                self.assertEqual(body["error_code"], "node_restore_busy")
                self.assertTrue(body["retryable"])
                # Answered without a live inspection, which could block behind
                # the very work that made the node busy: no parked record.
                self.assertNotIn("sandbox", body)
                self.assertEqual(service.get("sandbox").state, HibernationState.PARKED.value)
                with request.urlopen(wake(base), timeout=5) as response:
                    self.assertEqual(json.load(response)["sandbox"]["state"], HibernationState.RUNNING.value)

    def test_delete_of_a_replaced_generation_leaves_the_replacement(self) -> None:
        with TemporaryDirectory() as raw:
            provisioner, registry, *_ = self.make(Path(raw).resolve())
            service = DirectSandboxService(provisioner, process_runner=FakeProcessRunner())
            self.create(service, self.spec(), generation=8)
            current = registry.get("sandbox")
            with self.assertRaises(DirectRegistryError):
                provisioner.delete("sandbox", generation=7)
            self.assertEqual(registry.get("sandbox"), current)


if __name__ == "__main__":
    unittest.main()
