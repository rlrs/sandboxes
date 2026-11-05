"""Worker registrations that no route owns.

A create commits ``planned`` before it prepares storage. When a later step
fails, for example a workspace format timing out on an overloaded worker,
nothing advances or rolls back the registration, and the worker keeps its
reservation until someone deletes it. A create can also finish after its
route was deleted: a DELETE that reaches the worker before it registers the
sandbox finds nothing to delete (2026-11-04), and the sandbox then runs with
no route. The gateway's route is the authority: a planned incarnation it has
no route for, or one older than its route's generation, belongs to no create
and no client; so does an incarnation in any other state once its id has no
route at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Mapping

from .models import SandboxNode
from .routing import SandboxRoute

# A live create leaves ``planned`` within its storage-prepare bound (minutes);
# the candidate must stay stuck this long, continuously, before it is deleted.
STUCK_START_MIN_AGE = timedelta(minutes=20)

StuckStartKey = tuple[str, str, int, str]


@dataclass(frozen=True)
class StuckStart:
    job_id: str
    node_url: str
    sandbox_id: str
    generation: int
    operation_id: str
    state: str = "planned"

    @property
    def key(self) -> StuckStartKey:
        return (self.job_id, self.sandbox_id, self.generation, self.operation_id)


def unowned(state: str, generation: int, route: SandboxRoute | None) -> bool:
    """Whether no route owns a worker incarnation in ``state``.

    Past ``planned``, only a missing route counts: an incarnation older than
    its route may still be the source of a move to the newer one.
    """
    if route is None:
        return True
    return state == "planned" and route.generation > generation


def stuck_start_candidates(
    nodes: Iterable[SandboxNode], routes: Iterable[SandboxRoute], *, unrouted: bool = False,
) -> list[StuckStart]:
    """Registrations that no route owns, from fresh, complete inventories.

    Planned ones always; with ``unrouted``, also those in later states.
    """

    by_id = {route.sandbox_id: route for route in routes}
    candidates = []
    for node in nodes:
        heartbeat = node.heartbeat
        if heartbeat is None or not node.heartbeat_fresh or not heartbeat.inventory_complete:
            continue
        if not heartbeat.node_url:
            continue
        for entry in heartbeat.inventory:
            if entry.state != "planned" and not unrouted:
                continue
            if not unowned(entry.state, entry.generation, by_id.get(entry.sandbox_id)):
                continue
            candidates.append(StuckStart(
                job_id=node.job_id, node_url=heartbeat.node_url, sandbox_id=entry.sandbox_id,
                generation=entry.generation, operation_id=entry.operation_id, state=entry.state,
            ))
    return candidates


def stuck_starts_due(
    candidates: Iterable[StuckStart], first_seen: Mapping[StuckStartKey, datetime], now: datetime,
    *, budget: int, min_age: timedelta = STUCK_START_MIN_AGE,
) -> list[StuckStart]:
    """The longest-stuck candidates past ``min_age``, at most ``budget``.

    ``first_seen`` holds when each candidate was first seen in an unbroken run
    of cycles; one never seen is as new as ``now``.
    """

    due = [item for item in candidates if now - first_seen.get(item.key, now) >= min_age]
    due.sort(key=lambda item: (first_seen[item.key], item.key))
    return due[:max(0, budget)]
