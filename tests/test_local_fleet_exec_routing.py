"""S4: signed exec session routing and loss, over the local fleet harness.

Tier: contract. A worker that honors the gateway's signed prefix names each
session ``xr1.<route>.<random>``; the gateway then routes polls, stdin and
signals by heartbeat alone. A late push is not silence: the gateway pulls the
worker's heartbeat first. When the worker is silent the gateway answers for
it: 503 while the incarnation is still its own, 404 once deleted or replaced,
410 once the incarnation was lost. A fresh worker is the authority on its own
sessions. Every session must carry the prefix, so heartbeat intake refuses
worker IDs too long to sign.
"""

from dataclasses import replace
import signal
import unittest

from tests.harness import LocalFleet
from sandboxes.agent import fetch_node_agent_heartbeat, post_heartbeat_with_headers
from sandboxes.exec_session_routes import MAX_WORKER_ID_BYTES

TEST_TIER = "contract"


def _exec_paths(node) -> list[tuple[str, str]]:
    return [request for request in node.requests if request[1].startswith("/v1/exec/")]


class SignedExecSessionTests(unittest.TestCase):
    def test_answers_after_silence_deletion_replacement_and_loss(self):
        with LocalFleet() as fleet:
            node = fleet.nodes[0]
            fleet.create("alpha")
            first = fleet.start_exec("alpha", ["sleep", "30"]).json()["session"]["id"]
            fleet.expire_heartbeat(node)
            self.assertEqual(fleet.events(first, after=0, wait_seconds=0).status, 200)

            # Silent, but the incarnation is still this worker's: retryable,
            # and nothing reaches the worker.
            node.stop()
            fleet.expire_heartbeat(node)
            before = len(_exec_paths(node))
            for response in (
                fleet.events(first, after=0, wait_seconds=0),
                fleet.exec_stdin(first, "x"),
                fleet.exec_signal(first, int(signal.SIGTERM)),
            ):
                self.assertEqual(response.status, 503, response.body)
                self.assertEqual(response.json()["error_code"], "sandbox_worker_unreachable")
                self.assertTrue(response.json()["retryable"])
                self.assertEqual(response.headers["Retry-After"], "1")
            self.assertEqual(len(_exec_paths(node)), before)
            # The restarted agent keeps the sandbox; this scenario needs only
            # a session it started.
            node.start()
            first = fleet.start_exec("alpha", ["sleep", "30"]).json()["session"]["id"]

            # Deleted: the fresh worker still replays the killed command's
            # events, even though its inventory is now empty; once silent,
            # the gateway knows the incarnation is gone.
            self.assertEqual(fleet.delete("alpha").status, 200)
            fleet.heartbeat()
            replay = fleet.events(first, after=0, wait_seconds=0)
            self.assertEqual(replay.status, 200, replay.body)
            exit_event = replay.json()["events"][-1]
            self.assertEqual(exit_event["stream"], "exit")
            self.assertNotEqual(exit_event["exit_code"], 0)
            node.stop()
            fleet.expire_heartbeat(node)
            gone = fleet.events(first, after=0, wait_seconds=0)
            self.assertEqual((gone.status, gone.json()), (404, {"error": "exec route not found", "retryable": False}))

            # Replaced: the old generation's session is unknown, the new one
            # is retryable while its worker is silent.
            node.start()
            fleet.create("alpha")
            self.assertEqual(fleet.route("alpha").generation, 2)
            second = fleet.start_exec("alpha", ["sleep", "30"]).json()["session"]["id"]
            node.stop()
            fleet.expire_heartbeat(node)
            self.assertEqual(fleet.events(first, after=0, wait_seconds=0).status, 404)
            self.assertEqual(fleet.events(second, after=0, wait_seconds=0).status, 503)

            # Lost with its boot: the rebooted worker does not know the
            # session, and once it is silent the gateway reports the loss.
            node.reboot()
            fleet.heartbeat()
            self.assertIsNone(fleet.route("alpha"))
            unknown = fleet.events(second, after=0, wait_seconds=0)
            self.assertEqual(unknown.status, 404, unknown.body)
            self.assertIn("exec session not found", unknown.json()["error"])
            node.stop()
            fleet.expire_heartbeat(node)
            lost = fleet.events(second, after=0, wait_seconds=0)
            self.assertEqual(lost.status, 410, lost.body)
            self.assertEqual(
                {key: lost.json()[key] for key in ("error_code", "retryable", "sandbox_id", "sandbox_generation")},
                {"error_code": "exec_worker_lost", "retryable": False, "sandbox_id": "alpha",
                 "sandbox_generation": 2},
            )
            self.assertTrue(lost.json()["lost_at"])


    def test_heartbeat_intake_refuses_worker_ids_too_long_to_sign(self):
        with LocalFleet() as fleet:
            sample = fetch_node_agent_heartbeat(
                fleet.nodes[0].url, bearer_token=fleet.tokens.node_control
            )
            url = fleet.gateway_url + "/v1/nodes/heartbeat"
            auth = {"Authorization": f"Bearer {fleet.tokens.heartbeat}"}
            for field in ("node_id", "job_id"):
                with self.subTest(field):
                    oversized = replace(sample, **{field: "w" * (MAX_WORKER_ID_BYTES + 1)})
                    self.assertEqual(post_heartbeat_with_headers(url, oversized, auth).status, 403)
            self.assertEqual(post_heartbeat_with_headers(url, sample, auth).status, 200)


if __name__ == "__main__":
    unittest.main()
