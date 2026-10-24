"""The agent's side of runtime/noded's netns+veth pool.

noded owns ``pool`` in network-slots.json and its pairs. The agent leases
other slots and never touches the pool, but it finishes or releases a lease
whose hand-off from the pool a crash interrupted: the slot is the lease's,
while its pair still has the pool's name.
"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sandboxes.direct_network import DirectNetworkManager
from tests.support import FakeKernel


class PoolHandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name).resolve()
        self.kernel = FakeKernel(self.root / "netns")
        self.manager = self.kernel.install(DirectNetworkManager(
            self.root / "network-slots.json", namespace_root=self.root / "netns"))

    def state(self) -> dict:
        return json.loads((self.root / "network-slots.json").read_text())

    def noded_pooled(self, slot: int, **state) -> None:
        """The pair noded configured for ``slot``, and its state file."""
        namespace = f"ucloud-pool-{slot}"
        self.kernel.run(("ip", "netns", "add", namespace))
        self.kernel.run(("ip", "link", "add", f"us{slot}h", "netns", namespace))
        (self.root / "network-slots.json").write_text(json.dumps({"version": 1, **state}))

    def test_pooled_slots_are_never_leased_or_touched(self) -> None:
        self.noded_pooled(1, leases={}, pool=[1])
        lease = self.manager.ensure("a", 1)
        self.assertEqual(lease.slot, 2)
        self.assertEqual(self.state()["pool"], [1])
        self.assertIn("ucloud-pool-1", self.kernel.names())
        self.assertEqual(self.kernel.links["us1h"], self.kernel.namespace("ucloud-pool-1"))

    def test_an_interrupted_handoff_is_finished_by_the_next_ensure(self) -> None:
        self.noded_pooled(1, leases={"a\u00001": 1}, pool=[])
        lease = self.manager.ensure("a", 1)
        self.assertEqual(lease.slot, 1)
        self.assertEqual(self.kernel.links[lease.host_interface], self.kernel.namespace(lease.namespace))
        self.assertIn(lease.host_interface, self.kernel.configured)
        self.assertNotIn("ucloud-pool-1", self.kernel.names())

    def test_release_of_an_interrupted_handoff_drops_the_pooled_name(self) -> None:
        self.noded_pooled(1, leases={"a\u00001": 1}, pool=[])
        self.manager.release("a", 1)
        self.assertEqual(self.kernel.names(), set())
        self.assertEqual(self.kernel.links, {})
        self.assertEqual(self.state()["leases"], {})


if __name__ == "__main__":
    unittest.main()
