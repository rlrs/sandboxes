"""A stable age order of workers, oldest first."""

from __future__ import annotations

from .models import NodeHeartbeat


def consolidation_rank(node: NodeHeartbeat) -> tuple[int, str, str]:
    # UCloud job IDs are increasing decimal strings. This remains a stable
    # total order for other providers and does not depend on changing load.
    return len(node.job_id), node.job_id, node.node_id
