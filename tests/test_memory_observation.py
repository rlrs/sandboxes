import unittest

from sandboxes.models import (
    ResourceQuantity,
    SandboxInventoryEntry,
    SandboxMemoryObservation,
    utc_now,
)


class MemoryObservationTests(unittest.TestCase):
    """A sandbox's memory observation is optional advice in worker inventory."""

    def setUp(self):
        self.now = utc_now()
        self.entry = SandboxInventoryEntry(
            sandbox_id="s",
            generation=1,
            operation_id="create-s",
            spec_hash="a" * 64,
            state="parked",
            resources=ResourceQuantity(vcpu=2, memory_mb=2048, disk_mb=8000),
            memory_observation=SandboxMemoryObservation(
                512 * 1024**2, self.now.isoformat()
            ),
        )

    def test_impossible_advisory_memory_is_unknown_without_losing_ownership(self):
        for memory in (2**63, 10**1000, -1, True, 1.5):
            with self.subTest(memory_type=type(memory).__name__):
                raw = self.entry.to_dict()
                raw["memory_observation"] = {
                    "memory_bytes": memory,
                    "sampled_at": self.now.isoformat(),
                }
                parsed = SandboxInventoryEntry.from_dict(raw)
                self.assertEqual(parsed.sandbox_id, "s")
                self.assertIsNone(parsed.memory_observation)

    def test_inventory_observation_is_optional_and_malformed_advice_does_not_erase_owner(
        self,
    ):
        legacy = SandboxInventoryEntry.from_dict(
            {**self.entry.to_dict(), "memory_observation": None}
        )
        self.assertIsNone(legacy.memory_observation)
        self.assertNotIn("memory_observation", legacy.to_dict())
        self.assertEqual(
            SandboxInventoryEntry.from_dict(self.entry.to_dict()), self.entry
        )
        malformed = {**self.entry.to_dict(), "memory_observation": {"memory_bytes": -1}}
        parsed = SandboxInventoryEntry.from_dict(malformed)
        self.assertEqual(parsed.sandbox_id, "s")
        self.assertIsNone(parsed.memory_observation)
