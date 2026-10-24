"""The agent's cached registry index against the SQLite journal.

runtime/noded owns the registry file; the agent writes as a foreign process
(cached_reads) and serves reads from an index revalidated against SQLite. The
index must equal the committed state and never run backwards.
"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
import time
import os
import unittest

from sandboxes.direct_registry import DirectRegistryError, DirectSandboxRegistry
# Import the module, not the TestCase: discovery would rerun it here.
from tests import test_direct_registry as registry_fixtures

TEST_TIER = "contract"


class RegistryIndexTests(unittest.TestCase):
    fixtures = registry_fixtures.DirectRegistryTests()

    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name).resolve()
        self.path = self.root / "registry.sqlite"

    def cached(self) -> DirectSandboxRegistry:
        registry = DirectSandboxRegistry(self.path, cached_reads=True)
        self.addCleanup(registry.close)
        return registry

    def plan(self, registry, name: str, generation: int = 1):
        return registry.plan(
            spec=self.fixtures.spec(name), sandbox_generation=generation,
            operation_id=f"create:{generation}", runtime_compatibility_sha256="b" * 64,
        )

    def assert_matches_journal(self, owner: DirectSandboxRegistry) -> None:
        journal = DirectSandboxRegistry(self.path)
        self.assertEqual(owner.snapshot().records, journal.snapshot().records)
        self.assertEqual(owner.activity_revision(), journal.activity_revision())
        self.assertEqual(owner.disk_claims_mb(), journal.disk_claims_mb())

    def test_concurrent_writers_stay_coherent_in_the_cached_index(self) -> None:
        owner = self.cached()
        foreign = DirectSandboxRegistry(self.path)
        stop = Event()
        failures: list[str] = []

        def lifecycle(registry, prefix: str) -> None:
            for index in range(8):
                name = f"{prefix}{index}"
                if index % 2:
                    self.fixtures.delete_registration(registry, self.root, name, 1)
                else:
                    self.fixtures.owned_registration(registry, self.root, name, 1)

        def read() -> None:
            last = 0
            while not stop.is_set():
                snapshot = owner.snapshot()
                revision = snapshot.activity_revision
                if revision < last:
                    failures.append(f"revision went back from {last} to {revision}")
                last = revision
                if owner.activity_revision() < revision:
                    failures.append("clock behind its own snapshot")
                for record in snapshot.records:
                    current = owner.get(record.sandbox_id)
                    if current is not None and current.revision < record.revision:
                        failures.append(f"{record.sandbox_id} went back")
                time.sleep(0.001)  # Spinning readers would starve writers of the GIL.

        readers = [Thread(target=read) for _ in range(3)]
        for reader in readers:
            reader.start()
        try:
            with ThreadPoolExecutor(max_workers=5) as pool:
                jobs = [pool.submit(lifecycle, owner, f"o{index}-") for index in range(4)]
                jobs.append(pool.submit(lifecycle, foreign, "f-"))
                for job in jobs:
                    job.result()
        finally:
            stop.set()
            for reader in readers:
                reader.join(10)
        self.assertEqual(failures, [])
        owner.get("f-0", fresh=True)  # The other process's last commit.
        self.assert_matches_journal(owner)
        self.assertEqual(len(owner.snapshot().records), 20)

    def test_memoized_derivations_stay_out_of_the_record(self) -> None:
        owner = self.cached()
        record = self.fixtures.owned_registration(owner, self.root, "a", 1)
        cached = owner.get("a")
        self.assertIs(cached.to_direct_sandbox(), cached.to_direct_sandbox())
        self.assertEqual(cached.spec_sha256, record.spec_sha256)
        self.assertEqual(cached.to_dict(), record.to_dict())
        self.assertEqual(DirectSandboxRegistry._encode(cached), DirectSandboxRegistry._encode(record))
        self.assertEqual(type(cached).from_dict(cached.to_dict()), cached)

    def test_fork_child_must_reopen(self) -> None:
        owner = self.cached()
        self.plan(owner, "a")
        pid = os.fork()
        if pid == 0:
            try:
                owner.get("a")
            except DirectRegistryError:
                os._exit(0)
            os._exit(1)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0)
        self.assertEqual(owner.get("a").phase, "planned")


if __name__ == "__main__":
    unittest.main()
