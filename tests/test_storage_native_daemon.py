from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, replace
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from types import SimpleNamespace
import unittest.mock
from unittest.mock import patch
from typing import Callable

from sandboxes.storage_native import (
    StorageNativeDevice,
    StorageNativeDeviceOwner,
)
from sandboxes.storage_native_daemon import (
    LinuxStorageHostOperations,
    StorageNativeCapacityError,
    StorageNativeConflictError,
    StorageNativeNodeConfig,
    StorageNativeNodeClient,
    StorageNativeNodeError,
    StorageNativeJournal,
    StorageNativeNodeServer,
    StorageNativeNodeService,
    StorageVolumeOwner,
    StorageVolumeRecord,
    StorageVolumeState,
)
from sandboxes.storage_native_registry import (
    PublishedStorageLayer,
    StorageSnapshotPublication,
)

TEST_TIER = "contract"


class LinuxVolumeReadaheadTests(unittest.TestCase):
    def test_workspace_format_disables_only_supported_host_cpu_log_sizing(self):
        for log_help, expected in (
            ("/* log subvol */ [-l size=num,concurrency=num]\n/* label */", ("-l", "concurrency=0")),
            ("/* log subvol */ [-l size=num]\n/* label */", ()),
            # Data-device concurrency is not proof of log-option support.
            ("/* data subvol */ [-d concurrency=num]\n/* log subvol */ [-l size=num]\n/* label */", ()),
        ):
            with self.subTest(log_help=log_help):
                host = LinuxStorageHostOperations()
                result = subprocess.CompletedProcess(("mkfs.xfs",), 1, "", log_help)
                with (patch("sandboxes.storage_native_daemon.subprocess.run", return_value=result) as probe,
                      patch.object(host, "_run") as run):
                    host.format_xfs(Path("/dev/ublkb17"))
                    host.format_xfs(Path("/dev/ublkb18"))
                probe.assert_called_once()
                self.assertEqual(probe.call_args.args[0], ("mkfs.xfs", "-l", "help"))
                for call, device in zip(run.call_args_list, ("/dev/ublkb17", "/dev/ublkb18")):
                    self.assertEqual(call.args, ("mkfs.xfs", "-f", "-K", "-m", "reflink=1", "-n", "ftype=1", *expected, device))

    def test_failed_device_configuration_never_mounts(self):
        host = LinuxStorageHostOperations()
        with (patch("sandboxes.storage_native_daemon.os.open", side_effect=FileNotFoundError(2, "No such file or directory")),
              patch.object(host, "_mount_xfs") as mount):
            with self.assertRaisesRegex(StorageNativeNodeError, r"^blockdev failed with 2: No such file or directory$"):
                host.mount(Path("/dev/ublkb17"), Path("/volumes/first"))
        mount.assert_not_called()

    def test_readahead_and_mount_are_the_syscalls_blockdev_and_mount_made(self):
        host = LinuxStorageHostOperations()
        mount = unittest.mock.Mock(return_value=0)
        with (patch("sandboxes.storage_native_daemon.os.open", return_value=91) as opened,
              patch("sandboxes.storage_native_daemon.os.close") as closed,
              patch("sandboxes.storage_native_daemon.fcntl.ioctl") as ioctl,
              patch("sandboxes.storage_native_daemon._libc_mount", return_value=mount),
              patch("sandboxes.storage_native_daemon.os.sysconf", return_value=4096)):
            host.mount(Path("/dev/ublkb17"), Path("/volumes/first"))
        self.assertEqual(opened.call_args.args[0], Path("/dev/ublkb17"))
        ioctl.assert_called_once_with(91, 0x1262, 8)  # BLKRASET, 4 KiB in sectors
        closed.assert_called_once_with(91)
        # mount -t xfs -o noatime,nouuid: MS_NOATIME plus the XFS option.
        mount.assert_called_once_with(b"/dev/ublkb17", b"/volumes/first", b"xfs", 1024, b"nouuid")

    def test_mount_failure_keeps_the_error_shape(self):
        host = LinuxStorageHostOperations()
        with (patch("sandboxes.storage_native_daemon._libc_mount", return_value=lambda *args: -1),
              patch("sandboxes.storage_native_daemon.ctypes.get_errno", return_value=22)):
            with self.assertRaisesRegex(StorageNativeNodeError, r"^mount failed with 22: Invalid argument$"):
                host._mount_xfs(Path("/dev/ublkb17"), Path("/volumes/first"))

    def test_libc_mount_binding_passes_flags_as_unsigned_long(self):
        from sandboxes import storage_native_daemon as daemon
        import ctypes
        function = daemon._libc_mount()
        self.assertIs(daemon._libc_mount(), function)
        self.assertEqual(function.argtypes[3], ctypes.c_ulong)
        self.assertEqual(function.restype, ctypes.c_int)


@dataclass(frozen=True)
class FakeLayer:
    digest: str
    size: int
    uuid: str = ""


class FakeBlockBackend:
    def __init__(self, *, descriptor: bool = False, pooled: bool = False) -> None:
        self.descriptor = descriptor
        self.pooled = pooled
        self.live: set[int] = set()
        self.idle: set[int] = set()
        self.next_device_id = 1
        self.create_calls = 0
        self.restack_calls = 0
        self.delete_calls: list[int] = []
        self.release_calls: list[int] = []
        self.fail_next_release = False
        self.owners: dict[str, StorageNativeDeviceOwner] = {}
        self.device_sizes: dict[Path, int] = {}

    def create_runtime_device(
        self,
        *,
        source_image_config: Path,
        global_config: Path,
        runtime_dir: Path,
        virtual_size: int,
        upper_mode: str,
        owner_id: str,
    ) -> StorageNativeDevice:
        existing = self.owners.get(owner_id)
        if existing is not None:
            self.device_sizes[existing.device_path] = virtual_size
            return StorageNativeDevice(
                device_id=existing.device_id,
                device_path=existing.device_path,
                virtual_size=virtual_size,
                image_config_path=existing.image_config_path,
            )
        self.create_calls += 1
        if self.pooled and self.idle:
            device_id = min(self.idle)
            self.idle.remove(device_id)
        else:
            device_id = self.next_device_id
            self.next_device_id += 1
        self.live.add(device_id)
        runtime_dir.mkdir(mode=0o700)
        image = runtime_dir / "image.json"
        image.write_text("{}\n", encoding="ascii")
        device = StorageNativeDevice(
            device_id=device_id,
            device_path=Path(f"/dev/ublkb{device_id}"),
            virtual_size=virtual_size,
            image_config_path=image,
        )
        self.device_sizes[device.device_path] = virtual_size
        self.owners[owner_id] = StorageNativeDeviceOwner(
            owner_id=owner_id,
            device_id=device.device_id,
            device_path=device.device_path,
            image_config_path=device.image_config_path,
        )
        return device

    def list_runtime_device_owners(self) -> tuple[StorageNativeDeviceOwner, ...]:
        return tuple(self.owners.values())

    def restack_snapshot(self, device_id: int, output_layer_path: Path):
        if device_id not in self.live:
            raise RuntimeError("device is missing")
        self.restack_calls += 1
        output_layer_path.write_bytes(b"sealed-delta")
        if not self.descriptor:
            return None
        return FakeLayer(
            digest="sha256:" + "a" * 64,
            size=len(b"sealed-delta"),
        )

    def delete(self, device_id: int) -> None:
        self.delete_calls.append(device_id)
        self.idle.discard(device_id)
        self.live.discard(device_id)
        self.owners = {
            owner_id: owner
            for owner_id, owner in self.owners.items()
            if owner.device_id != device_id
        }

    def release(self, device_id: int) -> None:
        self.release_calls.append(device_id)
        if self.fail_next_release:
            self.fail_next_release = False
            raise RuntimeError("injected release failure")
        if device_id not in self.live:
            raise RuntimeError("device is missing")
        self.idle.add(device_id)
        self.owners = {
            owner_id: owner
            for owner_id, owner in self.owners.items()
            if owner.device_id != device_id
        }


class FakePublisher:
    def verify(
        self,
        publication: StorageSnapshotPublication,
    ) -> StorageSnapshotPublication:
        return publication

    def publish(
        self,
        *,
        exporter: object,
        source_layer_paths: tuple[Path, ...],
        virtual_size: int,
        existing_layers: tuple[PublishedStorageLayer, ...] = (),
        existing_repo_blob_url: str = "",
        global_config_path: Path | None = None,
        check_current: Callable[[], None] | None = None,
    ) -> StorageSnapshotPublication:
        if check_current is not None:
            check_current()
        if global_config_path is None:
            raise AssertionError("service did not supply its global config")
        new_layers = tuple(
            PublishedStorageLayer(
                digest="sha256:" + f"{index + 1:064x}",
                size=path.stat().st_size,
            )
            for index, path in enumerate(
                source_layer_paths,
                start=len(existing_layers),
            )
        )
        layers = (*existing_layers, *new_layers)
        return StorageSnapshotPublication(
            manifest_digest="sha256:" + "f" * 64,
            tag="ucloud-storage-v1-test",
            repository="snapshots",
            repo_blob_url="http://registry/v2/snapshots/blobs",
            virtual_size=virtual_size,
            layers=layers,
        )

    def metrics(self) -> dict[str, int]:
        return {"snapshot_publications": 0, "snapshot_compactions": 0}


class FakeHost:
    def __init__(self, backend: FakeBlockBackend) -> None:
        self.backend = backend
        self.mounted: set[Path] = set()
        self.frozen: set[Path] = set()
        self.formatted: list[Path] = []
        self.fail_next_unmount = False
        self.detached: list[Path] = []
        self.busy_devices: set[Path] = set()
        self.formatted_sizes: dict[Path, int | None] = {}
        self.filesystem_sizes: dict[Path, int] = {}
        self.mount_devices: dict[Path, Path] = {}
        self.grown: list[tuple[Path, int]] = []
        self.format_sizes: list[int | None] = []
        self.trimmed: list[Path] = []

    def device_is_unused(self, device: Path) -> bool:
        return device not in self.busy_devices

    def format_xfs(self, device: Path, *, size_bytes: int | None = None) -> None:
        self.formatted.append(device)
        self.formatted_sizes[device] = size_bytes
        self.format_sizes.append(size_bytes)

    def mount(self, device: Path, target: Path) -> None:
        self.mounted.add(target)
        if device in self.formatted_sizes:
            size = self.formatted_sizes.pop(device)
            self.filesystem_sizes[target] = size or self.backend.device_sizes[device]
        self.mount_devices[target] = device

    def trim(self, target: Path) -> None:
        self.trimmed.append(target)

    def filesystem_bytes(self, target: Path) -> int:
        if target in self.filesystem_sizes:
            return self.filesystem_sizes[target]
        # A snapshot mount inherits its filesystem; fakes carry no superblock.
        return self.backend.device_sizes[self.mount_devices[target]]

    def grow_xfs(self, target: Path, size_bytes: int) -> None:
        if target not in self.mounted or size_bytes < self.filesystem_bytes(target):
            raise RuntimeError("cannot grow")
        self.grown.append((target, size_bytes))
        self.filesystem_sizes[target] = size_bytes

    def sync(self, target: Path) -> None:
        if target not in self.mounted:
            raise RuntimeError("not mounted")

    def freeze(self, target: Path) -> None:
        self.frozen.add(target)

    def unfreeze(self, target: Path) -> None:
        self.frozen.remove(target)

    def unmount(self, target: Path) -> None:
        if self.fail_next_unmount:
            self.fail_next_unmount = False
            raise OSError("injected unreachable mount")
        self.mounted.remove(target)

    def detach(self, target: Path) -> None:
        self.detached.append(target)
        self.mounted.discard(target)

    def is_mounted(self, target: Path) -> bool:
        return target in self.mounted

    def ublk_device_ids(self) -> set[int]:
        return set(self.backend.live)


class StorageNativeNodeServiceTests(unittest.TestCase):
    def _retired_admission_fixture(self, root):
        service, backend, host = self._service(
            root, pooled=True, capacity=3 << 30, max_ublk_devices=1
        )
        owner = StorageVolumeOwner("retired", "retired", 1)
        created = service.converge_volume(
            owner, action="prepare", operation_id="create", virtual_size=1 << 30
        )
        host.busy_devices.add(Path(created.device_path))
        service.converge_volume(owner, action="release", operation_id="park")
        return service, backend, host, created

    def test_device_admission_rechecks_after_last_retirement_disappears(self):
        with TemporaryDirectory() as raw:
            service, backend, host, retired = self._retired_admission_fixture(Path(raw))
            host.busy_devices.clear()
            original_retired = service.journal.retired_devices
            reclaimed = False

            def retired_devices():
                nonlocal reclaimed
                if not reclaimed:
                    # Simulate metrics finishing reclamation after admission's
                    # first full-capacity snapshot, before it reads retirement.
                    reclaimed = True
                    backend.delete(retired.device_id)
                    service.journal.forget_retired_device(retired.device_owner_id)
                return original_retired()

            service.journal.retired_devices = retired_devices
            mounted = service.converge_volume(
                StorageVolumeOwner("next", "next", 1),
                action="prepare", operation_id="next", virtual_size=1 << 30,
            )
            self.assertEqual(mounted.state, StorageVolumeState.MOUNTED)
            self.assertEqual(backend.delete_calls, [retired.device_id])
            self.assertEqual(service._pending_device_allocations, 0)

    def test_device_admission_keeps_busy_retirement_charged_after_deadline(self):
        with TemporaryDirectory() as raw:
            service, backend, _, retired = self._retired_admission_fixture(Path(raw))
            service._DEVICE_RECLAIM_WAIT_SECONDS = 0.04
            with self.assertRaises(StorageNativeCapacityError):
                service.converge_volume(
                    StorageVolumeOwner("next", "next", 1),
                    action="prepare", operation_id="next", virtual_size=1 << 30,
                )
            self.assertEqual(backend.delete_calls, [])
            self.assertEqual(backend.release_calls, [])
            self.assertIn(retired.device_owner_id, backend.owners)
            self.assertEqual(len(service.journal.retired_devices()), 1)
            self.assertIsNone(service.journal.load("next"))
            self.assertEqual(service._pending_device_allocations, 0)

    def test_busy_device_is_retired_after_park_and_checkpoint_resumes(self):
        with TemporaryDirectory() as raw:
            service, backend, host = self._service(Path(raw), pooled=True)
            owner = StorageVolumeOwner("vol", "sandbox", 1)
            created = service.converge_volume(
                owner, action="prepare", operation_id="create", virtual_size=1 << 30
            )
            host.busy_devices.add(Path(created.device_path))
            released = service.converge_volume(
                owner, action="release", operation_id="park"
            )
            self.assertEqual(released.state, StorageVolumeState.RELEASED)
            self.assertEqual(backend.delete_calls, [])
            self.assertNotIn(created.device_id, backend.release_calls)
            self.assertTrue(all(Path(p).exists() for p in released.sealed_layer_paths))
            resumed = service.converge_volume(
                owner, action="mount", operation_id="wake"
            )
            self.assertEqual(resumed.state, StorageVolumeState.MOUNTED)
            self.assertNotEqual(resumed.device_id, created.device_id)
            restarted = StorageNativeNodeService(
                service.config,
                backend=backend,
                host=host,
                global_config_path=service.global_config_path,
            )
            restarted.reconcile()
            self.assertEqual(restarted.metrics()["retired_devices"], 1)
            self.assertEqual(backend.delete_calls, [])
            service.converge_volume(owner, action="delete", operation_id="delete")
            self.assertTrue(Path(released.sealed_layer_paths[0]).exists())
            self.assertEqual(restarted.metrics()["hard_reserved_bytes"], 1 << 30)
            host.busy_devices.clear()
            self.assertEqual(restarted.metrics()["retired_devices"], 0)
            self.assertEqual(backend.delete_calls, [created.device_id])
            self.assertFalse(Path(released.sealed_layer_paths[0]).exists())

    def test_busy_acquired_device_is_never_formatted_or_mounted(self):
        with TemporaryDirectory() as raw:
            service, backend, host = self._service(Path(raw), pooled=True)
            host.busy_devices.add(Path("/dev/ublkb1"))
            with self.assertRaisesRegex(StorageNativeNodeError, "still in use"):
                service.converge_volume(
                    StorageVolumeOwner("vol", "sandbox", 1),
                    action="prepare",
                    operation_id="create",
                    virtual_size=1 << 30,
                )
            self.assertEqual(host.formatted, [])
            self.assertEqual(host.mounted, set())
            self.assertEqual(backend.delete_calls, [])
            self.assertEqual(service.metrics()["retired_devices"], 1)
            self.assertEqual(backend.release_calls, [])

    def test_journal_v3_records_keep_their_full_size_charge(self):
        with TemporaryDirectory() as raw:
            service, _, _ = self._service(Path(raw))
            owner = StorageVolumeOwner("vol", "sandbox", 1)
            service.converge_volume(owner, action="prepare", operation_id="create",
                                    virtual_size=4 << 30, granted_size=1 << 30)
            with sqlite3.connect(service.journal.path) as connection:
                raw_json = json.loads(connection.execute(
                    "SELECT record_json FROM volumes").fetchone()[0])
                raw_json.pop("granted_size")
                raw_json.pop("local_layer_bytes")
                connection.execute("UPDATE volumes SET record_json = ?",
                                   (json.dumps(raw_json, sort_keys=True),))
                connection.executescript(
                    "DROP INDEX volumes_live_charges;"
                    "ALTER TABLE volumes DROP COLUMN charged_bytes; PRAGMA user_version=3;"
                )
            migrated = StorageNativeJournal(service.journal.path)
            # A pre-grant record was formatted at full size: charge it so.
            self.assertEqual(migrated.load("vol").granted_size, 4 << 30)
            with migrated._connection() as connection:
                self.assertEqual(migrated._active_reserved_bytes(connection), 4 << 30)

    def test_workspace_grant_bounds_filesystem_and_charge_until_grown(self):
        with TemporaryDirectory() as raw:
            service, _, host = self._service(Path(raw), capacity=(3 << 30) + (1 << 20))
            owner = StorageVolumeOwner("vol", "sandbox", 1)
            created = service.converge_volume(owner, action="prepare", operation_id="create",
                                              virtual_size=4 << 30, granted_size=1 << 30)
            self.assertEqual((created.virtual_size, created.granted_size), (4 << 30, 1 << 30))
            self.assertEqual(host.filesystem_bytes(Path(created.mount_path)), 1 << 30)
            self.assertEqual(host.format_sizes, [1 << 30])
            self.assertEqual(service.metrics()["hard_reserved_bytes"], 1 << 30)
            # Another volume may use the headroom a full-size charge used to hold.
            service.converge_volume(StorageVolumeOwner("other", "other", 1), action="prepare",
                                    operation_id="other", virtual_size=4 << 30, granted_size=1 << 30)
            grown = service.grow_volume(**owner.request_fields(), granted_size=2 << 30)
            self.assertEqual((grown.granted_size, grown.revision), (2 << 30, created.revision))
            self.assertEqual(host.grown, [(Path(created.mount_path), 2 << 30)])
            # Growth never shrinks, and repeats are idempotent.
            self.assertEqual(service.grow_volume(**owner.request_fields(), granted_size=1 << 30), grown)
            with self.assertRaises(StorageNativeCapacityError):
                service.grow_volume(**owner.request_fields(), granted_size=3 << 30)
            self.assertEqual(service.journal.load("vol").granted_size, 2 << 30)
            with self.assertRaises(ValueError):
                service.grow_volume(**owner.request_fields(), granted_size=5 << 30)
            with self.assertRaises(StorageNativeConflictError):
                service.grow_volume(sandbox_id="sandbox", sandbox_generation=2,
                                    volume_id="vol", granted_size=3 << 30)
            released = service.converge_volume(owner, action="release", operation_id="park")
            self.assertGreater(released.local_layer_bytes, 0)
            self.assertEqual(released.charged_bytes, (2 << 30) + released.local_layer_bytes)
            with self.assertRaisesRegex(StorageNativeConflictError, "not mounted"):
                service.grow_volume(**owner.request_fields(), granted_size=3 << 30)
            # The next mount reads the grant back from the filesystem itself.
            host.filesystem_sizes[Path(created.mount_path)] = (2 << 30) + (1 << 20)
            mounted = service.converge_volume(owner, action="mount", operation_id="wake")
            self.assertEqual(mounted.granted_size, (2 << 30) + (1 << 20))
            self.assertEqual(mounted.local_layer_bytes, released.local_layer_bytes)

    def test_seal_trims_only_when_the_upper_holds_freed_blocks(self):
        with TemporaryDirectory() as raw:
            service, _, host = self._service(Path(raw))
            owner = StorageVolumeOwner("vol", "sandbox", 1)
            live = service.converge_volume(owner, action="prepare", operation_id="create",
                                           virtual_size=4 << 30, granted_size=1 << 30)
            mount = Path(live.mount_path)
            service.config = replace(service.config, trim_before_seal_bytes=1 << 20)
            # The fake mount is a plain directory: report an empty filesystem.
            empty = SimpleNamespace(f_blocks=256, f_bfree=256, f_frsize=4096)
            statvfs = patch("sandboxes.storage_native_daemon.os.statvfs", return_value=empty)
            statvfs.start()
            self.addCleanup(statvfs.stop)
            captured = service.prepare_capture(**owner.request_fields(), operation_id="c1",
                                               expected_revision=live.revision)
            self.assertEqual(host.trimmed, [])  # the upper holds nothing freed
            service.abort_capture(**owner.request_fields(), operation_id="a1",
                                  expected_revision=captured.revision)
            # A punched filestore: the upper keeps 8 MiB the filesystem freed.
            (Path(live.runtime_dir) / "upper.data").write_bytes(b"u" * (8 << 20))
            current = service.journal.load("vol")
            service.prepare_capture(**owner.request_fields(), operation_id="c2",
                                    expected_revision=current.revision)
            self.assertEqual(host.trimmed, [mount])
            service.config = replace(service.config, trim_before_seal_bytes=0)
            current = service.journal.load("vol")
            service.abort_capture(**owner.request_fields(), operation_id="a2",
                                  expected_revision=current.revision)
            current = service.journal.load("vol")
            service.prepare_capture(**owner.request_fields(), operation_id="c3",
                                    expected_revision=current.revision)
            self.assertEqual(host.trimmed, [mount])

    def test_small_or_unaligned_grants_are_refused_and_full_grants_format_whole_device(self):
        with TemporaryDirectory() as raw:
            service, _, host = self._service(Path(raw))
            for granted in (256 << 20, (1 << 30) + 1):
                with self.assertRaises(ValueError):
                    service.converge_volume(StorageVolumeOwner("vol", "sandbox", 1), action="prepare",
                                            operation_id="create", virtual_size=4 << 30,
                                            granted_size=granted)
            whole = service.converge_volume(StorageVolumeOwner("whole", "sandbox", 1),
                                            action="prepare", operation_id="whole",
                                            virtual_size=1 << 30, granted_size=4 << 30)
            self.assertEqual(whole.granted_size, 1 << 30)
            self.assertEqual(host.format_sizes, [None])

    def test_capture_abort_retains_live_stack_and_fences_decisions(self):
        with TemporaryDirectory() as raw:
            service, backend, host = self._service(Path(raw))
            owner = StorageVolumeOwner("volume", "sandbox", 1)
            live = service.converge_volume(owner, action="prepare", operation_id="create", virtual_size=1 << 30)
            args = {**owner.request_fields(), "operation_id": "capture", "expected_revision": live.revision}
            captured = service.prepare_capture(**args)
            self.assertEqual(captured.state, StorageVolumeState.CAPTURE_PREPARED)
            self.assertEqual(captured.device_id, live.device_id)
            self.assertEqual(host.frozen, set())
            self.assertEqual(backend.restack_calls, 1)
            self.assertEqual(service.prepare_capture(**args), captured)
            service.reconcile()
            self.assertEqual(service.journal.load(owner.volume_id).state, StorageVolumeState.CAPTURE_PREPARED)
            with self.assertRaises(StorageNativeConflictError):
                service.release_runtime(**owner.request_fields(), operation_id="early-release", expected_revision=captured.revision)
            aborted = service.abort_capture(**owner.request_fields(), operation_id="abort", expected_revision=captured.revision)
            self.assertEqual(aborted.state, StorageVolumeState.MOUNTED)
            self.assertEqual(aborted.sealed_layer_paths, captured.sealed_layer_paths)
            self.assertTrue(all(Path(path).exists() for path in aborted.sealed_layer_paths))
            self.assertEqual(backend.delete_calls, [])
            with self.assertRaises(StorageNativeConflictError):
                service.commit_capture(**owner.request_fields(), operation_id="late-commit", expected_revision=captured.revision)
            second = service.prepare_capture(**owner.request_fields(), operation_id="capture-2", expected_revision=aborted.revision)
            self.assertEqual(len(second.sealed_layer_paths), 2)
            with self.assertRaisesRegex(StorageNativeConflictError, "superseded"):
                service.prepare_capture(**args)
            with self.assertRaisesRegex(StorageNativeConflictError, "superseded"):
                service.abort_capture(**owner.request_fields(), operation_id="abort", expected_revision=captured.revision)

            committed = service.commit_capture(**owner.request_fields(), operation_id="commit", expected_revision=second.revision)
            self.assertEqual(committed.state, StorageVolumeState.SEALED)
            released = service.release_runtime(**owner.request_fields(), operation_id="release", expected_revision=committed.revision)
            self.assertEqual(released.state, StorageVolumeState.RELEASED)
            with self.assertRaisesRegex(StorageNativeConflictError, "superseded"):
                service.commit_capture(**owner.request_fields(), operation_id="commit", expected_revision=second.revision)


    def test_capture_failure_is_fenced_and_thaws_filesystem(self):
        for failure in ("sync", "freeze", "snapshot", "thaw", "journal"):
            with self.subTest(failure=failure), TemporaryDirectory() as raw:
                service, backend, host = self._service(Path(raw))
                owner = StorageVolumeOwner("volume", "sandbox", 1)
                live = service.converge_volume(owner, action="prepare", operation_id="create", virtual_size=1 << 30)
                target, method = {
                    "sync": (host, "sync"), "freeze": (host, "freeze"),
                    "snapshot": (backend, "restack_snapshot"),
                    "thaw": (host, "unfreeze"), "journal": (service.journal, "finish"),
                }[failure]
                original = getattr(target, method)
                calls = []
                def fail_once(*args, **kwargs):
                    calls.append(None)
                    if len(calls) == 1:
                        raise RuntimeError("injected capture failure")
                    return original(*args, **kwargs)
                with patch.object(target, method, side_effect=fail_once):
                    with self.assertRaisesRegex(RuntimeError, "injected capture failure"):
                        service.prepare_capture(**owner.request_fields(), operation_id="capture", expected_revision=live.revision)
                failed = service.journal.load(owner.volume_id)
                self.assertEqual(failed.state, StorageVolumeState.ERROR)
                self.assertEqual(host.frozen, set())
                self.assertIn(live.device_id, backend.live)
                with self.assertRaises(StorageNativeConflictError):
                    service.abort_capture(**owner.request_fields(), operation_id="abort", expected_revision=failed.revision)

    def test_interrupted_capture_is_not_replayed_or_released(self):
        with TemporaryDirectory() as raw:
            service, backend, _ = self._service(Path(raw))
            owner = StorageVolumeOwner("volume", "sandbox", 1)
            live = service.converge_volume(owner, action="prepare", operation_id="create", virtual_size=1 << 30)
            service._begin_transition(**owner.request_fields(), operation_id="capture", expected_revision=live.revision,
                kind="PrepareCapture", allowed_states={StorageVolumeState.MOUNTED}, next_state=StorageVolumeState.PREPARING_CAPTURE)
            service.reconcile()
            self.assertEqual(service.journal.load(owner.volume_id).state, StorageVolumeState.ERROR)
            self.assertIn(live.device_id, backend.live)
            self.assertEqual(backend.restack_calls, 0)

    def _service(
        self,
        root: Path,
        *,
        capacity: int = 8 << 30,
        descriptor: bool = False,
        publisher: bool = False,
        pooled: bool = False,
        max_ublk_devices: int = 0,
    ) -> tuple[StorageNativeNodeService, FakeBlockBackend, FakeHost]:
        backend = FakeBlockBackend(descriptor=descriptor, pooled=pooled)
        host = FakeHost(backend)
        global_config = root / "global.json"
        global_config.write_text("{}\n", encoding="ascii")
        service = StorageNativeNodeService(
            StorageNativeNodeConfig(
                journal_path=root / "journal" / "storage.sqlite",
                runtime_root=root / "runtime",
                mount_root=root / "mounts",
                hard_capacity_bytes=capacity,
                device_pool_enabled=pooled,
                device_pool_low_watermark=(
                    min(2, max_ublk_devices) if max_ublk_devices > 0 else 2
                ),
                device_pool_high_watermark=(
                    min(16, max_ublk_devices) if max_ublk_devices > 0 else 16
                ),
                max_ublk_devices=max_ublk_devices,
            ),
            backend=backend,
            global_config_path=global_config,
            host=host,
            publisher=FakePublisher() if publisher else None,
        )
        return service, backend, host

    def test_failed_snapshot_mount_can_discard_without_releasing_recycled_device(self):
        from unittest.mock import patch

        with TemporaryDirectory() as raw:
            service, backend, host = self._service(Path(raw), pooled=True)
            owner = StorageVolumeOwner("vol", "sandbox", 1)
            service.converge_volume(
                owner, action="prepare", operation_id="create", virtual_size=1 << 30
            )
            released = service.converge_volume(
                owner, action="release", operation_id="park"
            )
            with patch.object(
                host, "mount", side_effect=RuntimeError("duplicate UUID")
            ):
                with self.assertRaisesRegex(RuntimeError, "duplicate UUID"):
                    service.converge_volume(owner, action="mount", operation_id="wake")
            failed = service.journal.load("vol")
            self.assertEqual(failed.state, StorageVolumeState.ERROR)
            other = StorageVolumeOwner("other", "other", 1)
            live = service.converge_volume(
                other, action="prepare", operation_id="other", virtual_size=1 << 30
            )
            self.assertEqual(live.device_id, failed.device_id)
            recovered = service.converge_volume(
                owner, action="discard", operation_id="rollback"
            )
            self.assertEqual(recovered.state, StorageVolumeState.RELEASED)
            self.assertEqual(recovered.sealed_layer_paths, released.sealed_layer_paths)
            self.assertIsNone(recovered.device_id)
            self.assertIn(live.device_owner_id, backend.owners)
            mounted = service.converge_volume(
                owner, action="mount", operation_id="retry"
            )
            self.assertEqual(mounted.state, StorageVolumeState.MOUNTED)
            self.assertNotEqual(mounted.device_id, live.device_id)

    def test_device_slot_failures_preserve_backend_ownership_accounting(self) -> None:
        for failure in ("before_acquire", "lost_acquire_response", "format"):
            with self.subTest(failure=failure), TemporaryDirectory() as raw:
                service, backend, host = self._service(
                    Path(raw), capacity=3 << 30, pooled=True, max_ublk_devices=1
                )
                acquire = backend.create_runtime_device
                format_xfs = host.format_xfs

                def failing_acquire(**kwargs):
                    if failure == "lost_acquire_response":
                        acquire(**kwargs)
                    raise RuntimeError("injected acquisition failure")

                def failing_format(device):
                    raise RuntimeError("injected format failure")

                if failure == "format":
                    host.format_xfs = failing_format
                else:
                    backend.create_runtime_device = failing_acquire
                # A failed rollback must remain charged to the real owner.
                backend.fail_next_release = failure != "before_acquire"

                def create(index):
                    return service.create_volume(
                        sandbox_id=f"sandbox-{index}",
                        sandbox_generation=1,
                        volume_id=f"volume-{index}",
                        operation_id=f"create:{index}",
                        virtual_size=1 << 30,
                    )

                with self.assertRaisesRegex(RuntimeError, "injected"):
                    create(1)
                self.assertEqual(service._pending_device_allocations, 0)
                backend.create_runtime_device = acquire
                host.format_xfs = format_xfs
                if failure == "before_acquire":
                    self.assertEqual(len(backend.owners), 0)
                else:
                    self.assertEqual(len(backend.owners), 1)
                    with self.assertRaises(StorageNativeCapacityError):
                        create(2)
                    first = service.journal.load("volume-1")
                    service.delete_volume(
                        sandbox_id="sandbox-1",
                        sandbox_generation=1,
                        volume_id="volume-1",
                        operation_id="delete:1",
                        expected_revision=first.revision,
                    )
                self.assertEqual(create(2).state, StorageVolumeState.MOUNTED)
                self.assertEqual(len(backend.owners), 1)
                self.assertEqual(service._pending_device_allocations, 0)

    def test_persisted_volume_requires_positive_accounting_id(self) -> None:
        with TemporaryDirectory() as raw:
            service, _, _ = self._service(Path(raw))
            created = service.create_volume(
                sandbox_id="sandbox-1",
                sandbox_generation=1,
                volume_id="volume-1",
                operation_id="create:1",
                virtual_size=1 << 20,
            ).to_json()

            missing = dict(created)
            missing.pop("accounting_id")
            with self.assertRaisesRegex(ValueError, "invalid schema"):
                StorageVolumeRecord.from_json(missing)

            zero = {**created, "accounting_id": 0}
            with self.assertRaisesRegex(ValueError, "positive accounting ID"):
                StorageVolumeRecord.from_json(zero)

            service.create_volume(
                sandbox_id="sandbox-2",
                sandbox_generation=1,
                volume_id="volume-2",
                operation_id="create:2",
                virtual_size=1 << 20,
            )
            with closing(sqlite3.connect(service.config.journal_path)) as connection:
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "UPDATE volumes SET accounting_id = 200000 "
                        "WHERE volume_id = 'volume-2'"
                    )
                connection.execute(
                    "UPDATE volumes SET accounting_id = 300000 "
                    "WHERE volume_id = 'volume-1'"
                )
                connection.commit()
            with self.assertRaisesRegex(
                StorageNativeNodeError,
                "columns are inconsistent",
            ):
                service.journal.load("volume-1")

    def test_existing_unversioned_journal_is_rejected(self) -> None:
        with TemporaryDirectory() as raw:
            path = Path(raw) / "journal" / "storage.sqlite"
            path.parent.mkdir(mode=0o700)
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("CREATE TABLE volumes (volume_id TEXT PRIMARY KEY)")
                connection.commit()

            with self.assertRaisesRegex(
                StorageNativeNodeError,
                "schema is incompatible",
            ):
                StorageNativeJournal(path)

    def test_local_lifecycle_supersedes_slow_publication_without_losing_authority(self):
        from unittest.mock import patch

        for action in ("mount", "discard", "delete"):
            for upload_fails in (False, True):
                with self.subTest(action=action, upload_fails=upload_fails), TemporaryDirectory() as raw:
                    service, _, _ = self._service(Path(raw), publisher=True)
                    owner = StorageVolumeOwner(sandbox_id="sandbox-1", sandbox_generation=1, volume_id="volume-1")
                    service.converge_volume(owner, action="prepare", operation_id="create", virtual_size=1 << 30)
                    released = service.converge_volume(owner, action="release", operation_id="park")
                    paths = tuple(Path(p) for p in released.sealed_layer_paths)
                    entered, finish = threading.Event(), threading.Event()
                    publish = service.publisher.publish

                    def slow_publish(**kwargs):
                        # Export has read the immutable inputs before its network stall.
                        result = publish(**kwargs)
                        entered.set()
                        if not finish.wait(5):
                            raise TimeoutError("test did not release upload")
                        if upload_fails:
                            raise OSError("upload failed")
                        return result

                    with patch.object(service.publisher, "publish", side_effect=slow_publish), ThreadPoolExecutor(max_workers=1) as pool:
                        future = pool.submit(service.converge_volume, owner, action="publish", operation_id="upload", expected_revision=released.revision)
                        try:
                            self.assertTrue(entered.wait(5))
                            with self.assertRaises(StorageNativeConflictError):
                                service.converge_volume(StorageVolumeOwner(sandbox_id="other", sandbox_generation=1, volume_id="volume-1"), action=action, operation_id="wrong-owner")
                            current = service.converge_volume(owner, action=action, operation_id="local")
                            self.assertEqual(current.state, {
                                "mount": StorageVolumeState.MOUNTED,
                                "discard": StorageVolumeState.RELEASED,
                                "delete": StorageVolumeState.DELETED,
                            }[action])
                            if action != "delete":
                                self.assertTrue(all(p.exists() for p in paths))
                            if action == "mount":
                                source = json.loads(Path(current.source_image_config).read_text())
                                self.assertEqual(source["lowers"], [{"file": str(p)} for p in paths])
                        finally:
                            finish.set()
                        with self.assertRaises((OSError, StorageNativeConflictError)):
                            future.result(timeout=5)
                    self.assertEqual(service.journal.load(owner.volume_id), current)
                    if action != "delete":
                        self.assertTrue(all(p.exists() for p in paths))

    def test_local_lifecycle_rereads_after_publication_moves_the_fence(self):
        # The wake reads RELEASED, then the background publication commits
        # RELEASED -> PUBLISHING before the wake fences its own transition.
        # (Discarding a released volume is a no-op and fences nothing.)
        from unittest.mock import patch

        for action in ("mount", "delete"):
            with self.subTest(action=action), TemporaryDirectory() as raw:
                service, _, _ = self._service(Path(raw), publisher=True)
                owner = StorageVolumeOwner(sandbox_id="sandbox-1", sandbox_generation=1, volume_id="volume-1")
                service.converge_volume(owner, action="prepare", operation_id="create", virtual_size=1 << 30)
                released = service.converge_volume(owner, action="release", operation_id="park")
                paths = tuple(Path(p) for p in released.sealed_layer_paths)
                entered, finish = threading.Event(), threading.Event()
                publish, load = service.publisher.publish, service.journal.load
                local = threading.get_ident()
                raced = []

                def slow_publish(**kwargs):
                    result = publish(**kwargs)
                    entered.set()
                    finish.wait(5)
                    return result

                def stale_load(volume_id):
                    record = load(volume_id)
                    if threading.get_ident() == local and not raced:
                        raced.append(record)
                        pool.submit(service.converge_volume, owner, action="publish",
                                    operation_id="upload", expected_revision=record.revision)
                        self.assertTrue(entered.wait(5))
                    return record

                with patch.object(service.publisher, "publish", side_effect=slow_publish), \
                     patch.object(service.journal, "load", side_effect=stale_load), \
                     ThreadPoolExecutor(max_workers=1) as pool:
                    try:
                        current = service.converge_volume(owner, action=action, operation_id="local")
                    finally:
                        finish.set()
                self.assertEqual(raced[0].state, StorageVolumeState.RELEASED)
                self.assertEqual(current.state, {
                    "mount": StorageVolumeState.MOUNTED,
                    "delete": StorageVolumeState.DELETED,
                }[action])
                self.assertEqual(service.journal.load(owner.volume_id), current)
                if action == "mount":
                    self.assertTrue(all(p.exists() for p in paths))

    def test_fenced_callers_do_not_retry_a_moved_revision(self):
        with TemporaryDirectory() as raw:
            service, _, _ = self._service(Path(raw), publisher=True)
            owner = StorageVolumeOwner(sandbox_id="sandbox-1", sandbox_generation=1, volume_id="volume-1")
            service.converge_volume(owner, action="prepare", operation_id="create", virtual_size=1 << 30)
            released = service.converge_volume(owner, action="release", operation_id="park")
            with self.assertRaises(StorageNativeConflictError):
                service.converge_volume(owner, action="mount", operation_id="stale",
                                        expected_revision=released.revision - 1)
            self.assertEqual(service.journal.load(owner.volume_id).state, StorageVolumeState.RELEASED)

    def test_reconcile_deletes_orphans_and_terminally_fences_missing_device(
        self,
    ) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            service, backend, _ = self._service(root)
            service.create_volume(
                sandbox_id="sandbox-1",
                sandbox_generation=1,
                volume_id="volume-1",
                operation_id="create:1",
                virtual_size=1 << 30,
            )
            backend.live.remove(1)
            backend.live.add(99)
            backend.owners["device:rogue"] = StorageNativeDeviceOwner(
                owner_id="device:rogue",
                device_id=99,
                device_path=Path("/dev/ublkb99"),
                image_config_path=root / "rogue" / "image.json",
            )
            result = service.reconcile()
            self.assertEqual(result["deleted_orphan_device_ids"], [99])
            self.assertEqual(result["terminal_records"][0]["record"]["state"], "error")
            record = service.journal.load("volume-1")
            assert record is not None
            self.assertEqual(record.state, StorageVolumeState.ERROR)

    def test_interrupted_create_is_not_blindly_replayed(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            service, backend, _ = self._service(root)
            volume_root = root / "runtime" / "volume-1"
            record = StorageVolumeRecord(
                volume_id="volume-1",
                sandbox_id="sandbox-1",
                sandbox_generation=1,
                revision=1,
                state=StorageVolumeState.CREATING,
                operation_id="create:1",
                virtual_size=1 << 30,
                runtime_dir=str(volume_root / "runtime"),
                mount_path=str(root / "mounts" / "volume-1"),
                source_image_config=str(volume_root / "source.json"),
                device_owner_id="device:test-interrupted-create",
                updated_ns=time.time_ns(),
            )
            request = {
                "kind": "CreateVolume",
                "operation_id": "create:1",
                "sandbox_generation": 1,
                "sandbox_id": "sandbox-1",
                "virtual_size": 1 << 30,
                "volume_id": "volume-1",
            }
            service.journal.reserve_create(
                request=request,
                record=record,
                hard_capacity_bytes=service.config.hard_capacity_bytes,
            )
            backend.live.add(7)
            backend.owners[record.device_owner_id] = StorageNativeDeviceOwner(
                owner_id=record.device_owner_id,
                device_id=7,
                device_path=Path("/dev/ublkb7"),
                image_config_path=volume_root / "runtime" / "image.json",
            )
            result = service.reconcile()
            self.assertEqual(backend.delete_calls, [7])
            self.assertEqual(result["terminal_records"][0]["record"]["state"], "error")

    def test_interrupted_cow_acquire_recovers_backend_owner_before_cleanup(
        self,
    ) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            service, backend, _ = self._service(root)
            volume_root = root / "runtime" / "volume-1"
            volume_root.mkdir(parents=True)
            source = volume_root / "source.json"
            source.write_text("{}\n", encoding="ascii")
            sealed = volume_root / "sealed.commit"
            sealed.write_bytes(b"sealed")
            record = StorageVolumeRecord(
                volume_id="volume-1",
                sandbox_id="sandbox-1",
                sandbox_generation=1,
                revision=1,
                state=StorageVolumeState.RELEASED,
                operation_id="seed:1",
                virtual_size=1 << 30,
                runtime_dir=str(volume_root / "runtime-seed"),
                mount_path=str(root / "mounts" / "volume-1"),
                source_image_config=str(source),
                device_owner_id="",
                sealed_layer_paths=(str(sealed),),
                updated_ns=time.time_ns(),
            )
            service.journal.reserve_create(
                request={
                    "kind": "CreateVolume",
                    "operation_id": "seed:1",
                    "sandbox_generation": 1,
                    "sandbox_id": "sandbox-1",
                    "virtual_size": 1 << 30,
                    "volume_id": "volume-1",
                },
                record=record,
                hard_capacity_bytes=service.config.hard_capacity_bytes,
            )
            pending = service.journal.begin_transition(
                request={
                    "expected_revision": 1,
                    "kind": "MountSnapshotCow",
                    "operation_id": "mount:1",
                    "sandbox_generation": 1,
                    "sandbox_id": "sandbox-1",
                    "volume_id": "volume-1",
                },
                operation_id="mount:1",
                kind="MountSnapshotCow",
                volume_id="volume-1",
                sandbox_id="sandbox-1",
                sandbox_generation=1,
                expected_revision=1,
                allowed_states={StorageVolumeState.RELEASED},
                next_state=StorageVolumeState.ACQUIRING,
                reserve_capacity=True,
                hard_capacity_bytes=service.config.hard_capacity_bytes,
            )
            assert isinstance(pending, StorageVolumeRecord)
            runtime_dir = volume_root / "runtime-2"
            pending = replace(
                pending,
                runtime_dir=str(runtime_dir),
                device_owner_id="device:test-interrupted-cow",
            )
            service.journal.update_pending(pending)
            backend.create_runtime_device(
                source_image_config=source,
                global_config=service.global_config_path,
                runtime_dir=runtime_dir,
                virtual_size=1 << 30,
                upper_mode=service.config.upper_mode,
                owner_id=pending.device_owner_id,
            )

            result = service.reconcile()

            recovered = service.journal.load("volume-1")
            assert recovered is not None
            self.assertEqual(recovered.state, StorageVolumeState.ERROR)
            self.assertEqual(result["terminal_records"][0]["record"]["state"], "error")
            self.assertEqual(backend.delete_calls, [1])
            self.assertEqual(backend.owners, {})

    def test_versioned_unix_socket_protocol(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            # Two 1 GiB grants plus volume-1's sealed layer blocks.
            service, backend, _ = self._service(root, capacity=(2 << 30) + (1 << 20))
            socket_path = root / "service" / "storage.sock"
            server = StorageNativeNodeServer(
                socket_path,
                service,
                require_root_peer=False,
            )
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            client = StorageNativeNodeClient(socket_path, timeout_seconds=2)
            deadline = time.monotonic() + 2
            while True:
                try:
                    features = client.get_features()
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.01)
            self.assertEqual(features["protocol_schema"], 4)
            self.assertEqual(features["upper_mode"], "hybridLogStructured")
            with self.assertRaisesRegex(StorageNativeNodeError, "invalid schema"):
                client._call({"operation": "GetFeatures", "unexpected": True})
            created = client.prepare_volume(
                StorageVolumeOwner("volume-1", "sandbox-1", 4),
                operation_id="create:1",
                virtual_size=1 << 30,
            )
            self.assertEqual(created.state, StorageVolumeState.MOUNTED)
            owner = StorageVolumeOwner("volume-1", "sandbox-1", 4)
            self.assertEqual(client.grow_volume(owner, granted_size=1 << 30), created)
            prepared = client.prepare_capture(owner, operation_id="capture", expected_revision=created.revision)
            self.assertEqual(prepared.state, StorageVolumeState.CAPTURE_PREPARED)
            aborted = client.abort_capture(owner, operation_id="abort", expected_revision=prepared.revision)
            self.assertEqual(aborted.state, StorageVolumeState.MOUNTED)
            prepared = client.prepare_capture(owner, operation_id="capture-again", expected_revision=aborted.revision)
            sealed = client.commit_capture(owner, operation_id="commit", expected_revision=prepared.revision)
            self.assertEqual(sealed.state, StorageVolumeState.SEALED)
            capture_released = client.ensure_released(owner, operation_id="release-capture")
            other = client.prepare_volume(
                StorageVolumeOwner("volume-2", "sandbox-2", 4),
                operation_id="create:1",
                virtual_size=1 << 30,
            )
            self.assertEqual(other.accounting_id, created.accounting_id + 1)
            with self.assertRaisesRegex(
                StorageNativeCapacityError,
                "hard capacity is exhausted",
            ):
                client.prepare_volume(
                    StorageVolumeOwner("volume-3", "sandbox-3", 4),
                    operation_id="create:3",
                    virtual_size=1 << 30,
                )
            metrics = client.get_metrics()
            self.assertEqual(
                metrics["hard_reserved_bytes"],
                (2 << 30) + service.journal.load("volume-1").local_layer_bytes,
            )
            self.assertEqual(metrics["active_operations"], 0)
            self.assertEqual(metrics["waiting_operations"], 0)
            self.assertEqual(
                client.get_volume("volume-1").revision,
                capture_released.revision,
            )
            released = client.ensure_released(
                created.owner,
                operation_id="park:1",
            )
            self.assertEqual(released.state, StorageVolumeState.RELEASED)
            mounted = client.ensure_mounted(
                created.owner,
                operation_id="wake:1",
            )
            self.assertEqual(mounted.state, StorageVolumeState.MOUNTED)
            self.assertEqual(backend.create_calls, 3)
            server.shutdown()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
