"""Registry capacity checks and snapshots stay cheap at node density.

Capacity checks run under SQLite's node-wide writer lock and snapshots run in
one-second loops; neither may decode every registration's JSON.
"""

from contextlib import closing
import sqlite3
import unittest
import unittest.mock

from sandboxes.direct_registry import DirectRegistryError
from tests import test_published_workspace_capacity as published

TEST_TIER = "contract"


class RegistryHotPathTests(unittest.TestCase):
    def setUp(self):
        fixture = published.PublishedWorkspaceCapacityTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.registry = fixture.registry
        self.workspace = fixture.workspace
        self._release, self._owned = fixture._release, fixture._owned
        self.registry.hard_disk_capacity_mb = 10 * fixture.claim
        fixture._plan("planned")  # a claim before its quota is committed

    def _json_reserved_mb(self):
        # The pre-ledger computation, straight from each record.
        return sum(
            record.quota_total_mb
            if record.quota_total_mb is not None
            else record.spec.requested_resources().disk_mb
            for record in self.registry.snapshot().records
        )

    def _ledger_reserved_mb(self):
        with self.registry._transaction(write=False) as connection:
            return self.registry._reserved_disk_bytes(connection) // 1024**2

    def test_ledger_matches_records_through_lifecycle(self):
        self.assertEqual(self._ledger_reserved_mb(), self._json_reserved_mb())
        self.assertTrue(self._release())
        self.assertEqual(
            self._ledger_reserved_mb(), self._json_reserved_mb() - self.workspace
        )
        self.registry.reserve_workspace_for_mount("one", 1)
        self.assertEqual(self._ledger_reserved_mb(), self._json_reserved_mb())

    def test_changed_schema_or_metadata_falls_back_to_validation(self):
        self.registry.get("one")
        with closing(sqlite3.connect(self.registry.path)) as conn:
            conn.execute("PRAGMA ignore_check_constraints = ON")
            conn.execute("UPDATE registry_metadata SET activity_revision = -1")
            conn.commit()
        with self.assertRaisesRegex(DirectRegistryError, "metadata"):
            self.registry.get("one")
        with closing(sqlite3.connect(self.registry.path)) as conn:
            conn.execute("DROP TABLE registry_metadata")
        with self.assertRaisesRegex(DirectRegistryError, "schema"):
            self.registry.get("one")


if __name__ == "__main__":
    unittest.main()
