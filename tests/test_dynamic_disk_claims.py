"""Dynamic disk claims: charge what a sandbox has demonstrated, never more
than the node physically has.

Workspace grants and park-time checkpoint space move a registration's claim;
fixed (legacy and upgraded) registrations keep their lifetime claim.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sandboxes.direct_registry import (
    DirectRegistryCapacityUnavailable,
    DirectSandboxRegistry,
    DiskClaim,
)
from sandboxes.direct_warden import DirectSandbox
from sandboxes.sandbox import SandboxSpec

TEST_TIER = "contract"


def _spec(name):
    return SandboxSpec(id=name, image="registry/image@sha256:" + "a" * 64,
                       memory_mb=1024, disk_mb=4096, parkable=True)


class DynamicDiskClaimTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.registry = DirectSandboxRegistry(self.root / "registry.sqlite",
                                              hard_disk_capacity_mb=4000)

    def _plan(self, name, claim=DiskClaim(1024, 64), split=True):
        return self.registry.plan(
            spec=_spec(name), sandbox_generation=1, operation_id="create:" + name,
            runtime_compatibility_sha256="b" * 64, split_memory_backing=split,
            initial_claim=claim,
        )

    def _owned(self, name, claim=DiskClaim(1024, 64)):
        planned = self._plan(name, claim)
        quota = self.registry.commit_quota(
            name, expected_revision=planned.revision, project_id=200000,
            total_mb=planned.spec.requested_resources().disk_mb, quota_path=self.root / name,
        )
        incarnation = f"{name}.sandbox-1"
        rootfs = self.registry.commit_rootfs(
            name, expected_revision=quota.revision, image_id="sha256:" + "e" * 64,
            sandbox=DirectSandbox(
                sandbox_id=name, sandbox_generation=1, container_id="f" * 64,
                spec_sha256=quota.spec_sha256, rootfs_sha256="d" * 64,
                bundle=self.root / "bundles" / name, memory_directory=incarnation,
                workspace_directory="workspace-" + incarnation,
                memory=planned.memory_reference,
            ),
        )
        return self.registry.commit_owned(name, expected_revision=rootfs.revision)

    def _reserved_mb(self):
        with self.registry._transaction(write=False) as connection:
            return self.registry._reserved_disk_bytes(connection) // 1024**2

    def test_plans_charge_the_initial_claim_not_the_maximum(self):
        # The 7,232 MiB maximum would not fit twice; two initial claims do.
        self._plan("one")
        self._plan("two")
        self.assertEqual(self._reserved_mb(), 2 * 1088)
        self.assertEqual(self.registry.disk_claim("one", 1), DiskClaim(1024, 64))
        self.assertEqual(self.registry.disk_claims_mb(), {("one", 1): 1088, ("two", 1): 1088})
        with self.assertRaises(DirectRegistryCapacityUnavailable):
            self._plan("three", DiskClaim(2000, 64))
        with self.assertRaises(ValueError):
            self._plan("legacy", split=False)

    def test_published_workspace_releases_the_dynamic_grant_and_remount_recharges_it(self):
        self._owned("one")
        self.registry.update_disk_claim("one", 1, workspace_mb=1500)
        # The caller passes the ceiling; the dynamic row releases its own claim.
        self.assertTrue(self.registry.release_published_workspace(
            "one", 1, workspace_mb=4096, expected_mount_epoch=0))
        self.assertEqual(self._reserved_mb(), 64)
        self.assertEqual(self.registry.disk_claims_mb(), {("one", 1): 64})
        # While published, workspace updates do not charge; remount does.
        self.registry.update_disk_claim("one", 1, workspace_mb=1024, require_capacity=True)
        self.assertEqual(self._reserved_mb(), 64)
        self.registry.hard_disk_capacity_mb = 1000
        with self.assertRaises(DirectRegistryCapacityUnavailable):
            self.registry.reserve_workspace_for_mount("one", 1)
        self.registry.hard_disk_capacity_mb = 4000
        self.registry.reserve_workspace_for_mount("one", 1)
        self.assertEqual(self._reserved_mb(), 1024 + 64)

    def test_fixed_claims_ignore_updates(self):
        self.registry.hard_disk_capacity_mb = 10**6
        self.registry.plan(spec=_spec("legacy"), sandbox_generation=1, operation_id="create:legacy",
                           runtime_compatibility_sha256="b" * 64, split_memory_backing=True)
        self.assertIsNone(self.registry.disk_claim("legacy", 1))
        self.assertIsNone(self.registry.update_disk_claim("legacy", 1, workspace_mb=1))
        self.assertEqual(self._reserved_mb(), _spec("legacy").requested_resources().disk_mb)


if __name__ == "__main__":
    unittest.main()
