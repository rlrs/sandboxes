"""What the agent's create configuration tells noded about the host.

noded reads the proc root and the overlay's mount commands from the agent
(``create_handoff.host_interfaces``) instead of assuming ``/proc`` and
``mount``; runtime/noded/src/pipeline.rs pins its side.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sandboxes.create_handoff import host_interfaces
from sandboxes.direct_warden import DirectRunscWardenConfig
from sandboxes.image_rootfs import OverlayRootfsManager
from tests.harness.assembly import FINGERPRINT


def warden_config(root: Path, **options) -> DirectRunscWardenConfig:
    return DirectRunscWardenConfig(
        runsc=root / "runsc", runtime_root=root / "runtime", memory_root=root / "memory",
        bundle_root=root / "bundles", journal_root=root / "journals", runtime_fingerprint=FINGERPRINT, **options)


def overlays(root: Path, **options) -> OverlayRootfsManager:
    return OverlayRootfsManager(None, writable_root=root / "volumes", bundle_root=root / "bundles", **options)


class HostInterfacesTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()

    def test_production_reports_the_host_proc_and_mount_commands(self) -> None:
        self.assertEqual(host_interfaces(warden_config(self.root), overlays(self.root)), {
            "proc_root": "/proc", "mount_binary": "mount", "umount_binary": "umount",
            "mountpoint_binary": "mountpoint",
        })

    def test_an_injected_proc_root_and_mount_commands_are_reported(self) -> None:
        bin_dir = self.root / "bin"
        reported = host_interfaces(
            warden_config(self.root, proc_root=self.root / "proc"),
            overlays(self.root, mount_binary=str(bin_dir / "mount"), umount_binary=str(bin_dir / "umount"),
                     mountpoint_binary=str(bin_dir / "mountpoint")),
        )
        self.assertEqual(reported, {
            "proc_root": str(self.root / "proc"), "mount_binary": str(bin_dir / "mount"),
            "umount_binary": str(bin_dir / "umount"), "mountpoint_binary": str(bin_dir / "mountpoint"),
        })


if __name__ == "__main__":
    unittest.main()
