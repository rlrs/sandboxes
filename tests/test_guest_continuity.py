"""VM readiness/silence never grants authority to destroy a UCloud guest."""

from dataclasses import replace
import unittest
from unittest.mock import patch

from tests.test_cli import (
    owned_heartbeat,
    sandbox_route,
    temporary_root,
)
from sandboxes import cli
from sandboxes.control_state import (
    ControlStateStore, QUARANTINE_REASON,
)
from sandboxes.models import (
    SandboxInventoryEntry,
    ScalePolicy,
    utc_now,
)

TEST_TIER = "contract"


def continuity_pair():
    route = sandbox_route(
        "sandbox", node_id="node-owned", job_id="owned", node_epoch="boot-a"
    )
    heartbeat = owned_heartbeat(
        node_epoch="boot-a",
        inventory_complete=True,
        active_sandboxes=1,
        received_at=utc_now(),
        inventory=(
            SandboxInventoryEntry(
                sandbox_id=route.sandbox_id,
                generation=route.generation,
                operation_id=route.create_operation_id,
                spec_hash=route.spec_hash,
                state="running",
                resources=route.resources,
            ),
        ),
    )
    return heartbeat, route


class GuestContinuityTests(unittest.TestCase):
    def continuity_cycle(self, store, job, probe, route, routing=None):
        with patch.object(cli, "_probe_unreachable_node", return_value=probe) as probed:
            jobs, _ = cli._quarantine_unverified_guests(
                [job], store.load_heartbeats(), control_state=store, policy=ScalePolicy(),
                deployment_id="prod-a", route_reservations={"owned": (route,)},
                execution_authorized=True, bearer_token="test-token", routing_store=routing,
            )
        return jobs[0].phase, probed.call_count

    def test_quarantine_survives_push_restart_and_rejects_obsolete_recovery(self):
        with temporary_root() as root:
            path = root / "control-state.sqlite"
            store = ControlStateStore(path)
            heartbeat, _ = continuity_pair()
            store.receive_heartbeat(heartbeat)
            store.quarantine_node("owned", "unverified")
            newer = replace(
                heartbeat, activity_epoch=10, updated_at=utc_now(), labels={}
            )
            self.assertFalse(store.receive_heartbeat(newer).stored.admission_open)
            restarted = ControlStateStore(path)
            self.assertFalse(restarted.get_heartbeat("owned").admission_open)
            self.assertFalse(restarted.recover_quarantined_node(heartbeat))
            self.assertTrue(restarted.recover_quarantined_node(newer))
            self.assertTrue(restarted.get_heartbeat("owned").admission_open)
            # A worker cannot invent controller-owned quarantine metadata either.
            store.receive_heartbeat(
                replace(newer, labels={QUARANTINE_REASON: "spoofed"})
            )
            self.assertNotIn(QUARANTINE_REASON, store.get_heartbeat("owned").labels)

    def test_incomplete_or_wrong_incarnation_inventory_cannot_reopen_placement(self):
        heartbeat, route = continuity_pair()
        entry = heartbeat.inventory[0]
        invalid = [
            replace(heartbeat, inventory_complete=False),
            replace(heartbeat, inventory=()),
            replace(heartbeat, node_epoch="new-boot"),
            replace(heartbeat, inventory=(replace(entry, generation=2),)),
            replace(heartbeat, inventory=(replace(entry, operation_id="other"),)),
            replace(heartbeat, inventory=(replace(entry, spec_hash="b" * 64),)),
            replace(heartbeat, inventory=(entry, entry)),
            replace(heartbeat, active_sandbox_creates=1),
        ]
        for fresh in invalid:
            with self.subTest(fresh=fresh):
                self.assertFalse(
                    cli._guest_continuity_matches(heartbeat, fresh, (route,))
                )
        self.assertTrue(cli._guest_continuity_matches(heartbeat, heartbeat, (route,)))
