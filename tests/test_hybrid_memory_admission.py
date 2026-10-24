"""Real allocation/journal placement changes precede admission cost quotes."""
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import test_split_memory_lifecycle as fixtures
from tests.admission_backends import AdmissionBackendCase
from sandboxes.direct_registry import DirectSandboxRegistry
from sandboxes.direct_service import DirectSandboxService
from sandboxes.hibernation import HibernationState
from sandboxes.memory_backing import MemoryBackingStore
from sandboxes.models import NodeRuntimeMetrics, ResourceQuantity, utc_now
from sandboxes.resource_evidence import MemoryBackingCapacity
from sandboxes.sandbox import SandboxSpec

# The noded half starts ucloud-noded (tests/noded_admission.py).
TEST_TIER = "contract"


class HybridMemoryAdmissionTests(AdmissionBackendCase, unittest.TestCase):
    def setUp(self):
        f = self.fixture = fixtures.SplitLifecycleTests()
        f.setUp()
        self.addCleanup(f.tearDown)
        registry = self.registry = DirectSandboxRegistry(f.root / "registry.sqlite")
        spec = SandboxSpec(id=f.sandbox.sandbox_id, image="image", memory_mb=4096,
                           disk_mb=1024, parkable=True, managed_process=True)
        registration = registry.plan(spec=spec, sandbox_generation=1,
            operation_id="create", runtime_compatibility_sha256="a" * 64,
            split_memory_backing=True)
        f.sandbox = replace(f.sandbox, spec_sha256=registration.spec_sha256,
                            workspace_directory=registration.workspace_directory,
                            memory=registration.memory_reference)
        active_root = f.root / "ram"
        active_root.mkdir(mode=0o700)
        f.warden.config = replace(f.warden.config, application_memory_root=active_root)
        f.runner.memory_root = active_root
        f.storage = fixtures.SplitStorage(sandbox_id=spec.id, sandbox_generation=1,
            volume_id=f.sandbox.workspace_directory,
            mount_path=f.config.memory_root / f.sandbox.workspace_directory)
        f.storage.mount_path.mkdir()
        f.warden.storage = f.storage
        f.rootfs.events = f.storage.events
        with patch("sandboxes.memory_backing.subprocess.run",
                   return_value=SimpleNamespace(stdout="tmpfs rw,noswap\n")):
            f.warden.memory_backing = MemoryBackingStore(f.config.memory_root,
                f.root / "allocations.sqlite", hard_capacity_bytes=64 << 30,
                quota=fixtures.FakeQuota(), active_root=active_root)
        f.warden.memory_backing.prepare(registration.memory_reference,
                                       sandbox_id=spec.id, sandbox_generation=1)
        f.warden.memory_capacity = registry
        registration = registry.commit_quota(spec.id, expected_revision=registration.revision,
            project_id=1, total_mb=spec.requested_resources().disk_mb,
            quota_path=f.storage.mount_path)
        registration = registry.commit_rootfs(spec.id, expected_revision=registration.revision,
            image_id="sha256:" + "b" * 64, sandbox=f.sandbox)
        registry.commit_owned(spec.id, expected_revision=registration.revision)
        f.warden.create(f.sandbox, operation_id="create")
        self.capture_initial_checkpoint()
        self.assertEqual(f.warden.application_memory_mode(spec.id, 1), "ram")
        f.warden.config = replace(f.warden.config, reflink_memory_restore=True)
        self.service = self.admitted(DirectSandboxService(SimpleNamespace(warden=f.warden, registry=registry)))
        self.service.admission_wait_seconds = .05
        self.service.configure_active_capacity(ResourceQuantity(vcpu=8, memory_mb=16 << 10),
            runtime_metrics_provider=lambda: NodeRuntimeMetrics(collected_at=utc_now(),
                cpu_count=8, cpu_percent=0, memory_total_mb=16 << 10,
                memory_available_mb=12 << 10,
                memory_backing=MemoryBackingCapacity(total_bytes=8 << 30,
                    available_bytes=0, identity="ram-mount")))

    def capture_initial_checkpoint(self):
        self.fixture.warden.park(self.fixture.sandbox, operation_id="park-before-upgrade")

    def test_parked_continuation_selects_file_before_growth_admission(self):
        service, f = self.service, self.fixture
        key = (f.sandbox.sandbox_id, 1)
        self.registry.growth_intent(*key, action="wait", request_id="model-request")
        service.admit_managed_continuation(*key, "model-request")
        self.assertEqual(f.warden.application_memory_mode(*key), "file")
        self.assertTrue(self.registry.relay_wake_fence(*key, "model-request"))
        self.assertEqual(service.reclaim_demand()[1].ram_backing_bytes, 0)
        self.assertEqual(service._restore_cost(*key, ResourceQuantity(memory_mb=4096)).memory_bytes,
                         4 << 30)

    def test_absent_candidate_restore_rollback_selects_file_only_after_parked(self):
        f = self.fixture
        journal = f.warden._journal(f.sandbox)
        parked = journal.load()
        journal.begin_restore(operation_id="interrupted-restore", expected_revision=parked.revision)
        self.assertEqual(f.warden.application_memory_mode(f.sandbox.sandbox_id, 1), "ram")
        self.assertEqual(f.warden.reconcile(f.sandbox).state, HibernationState.PARKED)
        self.assertEqual(f.warden.application_memory_mode(f.sandbox.sandbox_id, 1), "file")

