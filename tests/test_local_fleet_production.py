"""Scenarios on the production configuration: noded in front of every agent.

Tier: contract. ``LocalFleet.production()`` runs PostgreSQL routing,
power-of-k placement and a real ``ucloud-noded`` on each node's port with
Rust creates, execs and the pause tier (tests/harness/README.md).
"""

import unittest

from tests.harness import LocalFleet, process_alive

TEST_TIER = "contract"


class ProductionFleetTests(unittest.TestCase):
    def test_noded_creates_and_execs_and_the_agent_serves_files_and_deletes(self):
        with LocalFleet.production() as fleet:
            created = fleet.create("alpha")
            self.assertEqual(created["state"], "running")
            node = fleet.node_for("alpha")
            pid = node.sentry_pid("alpha")
            self.assertTrue(process_alive(pid))
            self.assertEqual(node.registration("alpha").phase, "owned")

            image = fleet.exec("alpha", ["cat", "/opt/greeting"])
            self.assertEqual((image.exit_code, image.stdout), (0, "hello from the image\n"))
            uploaded = fleet.write_file("alpha", "/workspace/notes.txt", b"uploaded\x00bytes")
            self.assertEqual(uploaded.status, 200, uploaded.body)
            downloaded = fleet.read_file("alpha", "/workspace/notes.txt")
            self.assertEqual((downloaded.status, downloaded.body), (200, b"uploaded\x00bytes"))
            counted = fleet.exec("alpha", ["/bin/sh", "-c", "wc -c < notes.txt"], working_dir="/workspace")
            self.assertEqual((counted.exit_code, counted.stdout.strip()), (0, "14"))

            # noded answered the create and both execs: the agent never parsed
            # either request, only the file routes it still owns.
            parsed = {(method, path.split("?")[0]) for method, path in node.requests}
            self.assertNotIn(("POST", "/v1/sandboxes"), parsed)
            self.assertNotIn(("POST", "/v1/sandboxes/alpha/exec"), parsed)
            self.assertIn(("PUT", "/v1/sandboxes/alpha/files"), parsed)

            deleted = fleet.delete("alpha")
            self.assertEqual(deleted.status, 200, deleted.body)
            self.assertIsNone(fleet.route("alpha"))
            self.assertFalse(process_alive(pid))
            self.assertEqual(node.live_sentries(), [])
            self.assertIsNone(node.registration("alpha"))
            self.assertEqual(list(node.volumes.iterdir()), [])
            self.assertEqual(node.storage_backend.devices, {})


if __name__ == "__main__":
    unittest.main()
