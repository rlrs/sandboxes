"""Gateway-owned exec route resolution, independent of HTTP transport."""

from dataclasses import dataclass
from http import HTTPStatus
from typing import Callable, Optional

from .control_state import QUARANTINE_REASON
from .exec_session_routes import ExecSessionRoutes, SignedExecRoute
from .models import NodeHeartbeat, parse_iso_datetime, utc_now
from .routing import ExecRoute


@dataclass
class ExecRouteUnavailable(Exception):
    status: int
    payload: dict
    headers: dict | None = None


def heartbeat_proves_route_absent(
    heartbeat: NodeHeartbeat | None,
    *,
    sandbox_id: str | None = None,
    route_created_at: str,
    route_updated_at: str,
    heartbeat_ttl_seconds: int,
) -> bool:
    if heartbeat is None or heartbeat.labels.get(QUARANTINE_REASON):
        return False
    if not heartbeat.is_fresh(utc_now(), heartbeat_ttl_seconds):
        return False
    if heartbeat.active_sandboxes != 0:
        return False
    if (
        sandbox_id is not None
        and heartbeat.inventory_complete
        and any(item.sandbox_id == sandbox_id for item in heartbeat.inventory)
    ):
        return False
    reference = parse_iso_datetime(route_updated_at) or parse_iso_datetime(
        route_created_at
    )
    return reference is None or heartbeat.freshness_at >= reference


# Pulls one stale worker heartbeat; None once the stale boot was replaced.
Refresh = Optional[Callable[[NodeHeartbeat], Optional[NodeHeartbeat]]]


def _worker_unreachable(route: SignedExecRoute) -> ExecRouteUnavailable:
    return ExecRouteUnavailable(
        HTTPStatus.SERVICE_UNAVAILABLE,
        {
            "error": "sandbox worker heartbeat is stale or unavailable",
            "error_code": "sandbox_worker_unreachable",
            "retryable": True,
            "node_id": route.node_id,
            "job_id": route.job_id,
        },
        {"Retry-After": "1", "X-UCloud-Sandbox-Retryable": "true"},
    )


def _worker_lost(
    session_id: str, sandbox_id: str, generation: int, lost_at: str
) -> ExecRouteUnavailable:
    return ExecRouteUnavailable(
        HTTPStatus.GONE,
        {
            "error": "exec worker was lost; the accepted command cannot resume",
            "error_code": "exec_worker_lost",
            "retryable": False,
            "session_id": session_id,
            "sandbox_id": sandbox_id,
            "sandbox_generation": generation,
            "lost_at": lost_at,
        },
    )


class ExecRoutingService:
    """Routes exec sessions by the signed prefix their worker named them under."""

    def __init__(
        self,
        control_store,
        routing_store,
        heartbeat_ttl_seconds,
        session_routes: ExecSessionRoutes,
    ):
        self.control_store = control_store
        self.routing_store = routing_store
        self.heartbeat_ttl_seconds = heartbeat_ttl_seconds
        self.session_routes = session_routes

    def signed_prefix(self, route) -> str:
        """Prefix for a new session on this exact sandbox incarnation.

        Raises ExecRouteUnavailable rather than start a session that later
        requests could not route.
        """

        try:
            return self.session_routes.prefix(
                SignedExecRoute(
                    sandbox_id=route.sandbox_id,
                    sandbox_generation=route.generation,
                    node_id=route.node_id,
                    job_id=route.job_id,
                    issued_at=max(1, int(utc_now().timestamp())),
                )
            )
        except ValueError:
            # Heartbeat intake bounds worker IDs, so only a malformed route
            # (for example a huge generation) can get here.
            raise ExecRouteUnavailable(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "error": "sandbox route identity is too long to sign an exec session",
                    "error_code": "exec_route_unsignable",
                    "retryable": False,
                },
            ) from None

    def fresh(self, heartbeat: NodeHeartbeat | None) -> bool:
        return bool(
            heartbeat is not None
            and heartbeat.node_url
            and heartbeat.is_fresh(utc_now(), self.heartbeat_ttl_seconds)
        )

    def heartbeat(self, route: SignedExecRoute, refresh: Refresh = None) -> NodeHeartbeat | None:
        heartbeat = self.control_store.get_heartbeat(
            route.job_id, include_inventory=False
        )
        if refresh is not None and heartbeat is not None and not self.fresh(heartbeat):
            # Silence is never loss: ask the worker once before a 503.
            heartbeat = refresh(heartbeat)
        return heartbeat

    def resolve(self, session_id: str, *, refresh: Refresh = None) -> tuple[ExecRoute, NodeHeartbeat]:
        """Route one session by its signed worker identity.

        ``refresh`` pulls a stale worker's heartbeat. Routing state is read
        only on failure. A fresh worker is the authority on its own sessions
        and answers an unknown one with 404. No heartbeat inventory can prove
        absence here: a heartbeat captured before the sandbox existed may be
        received after the prefix was minted.
        """

        signed = self.session_routes.decode(session_id)
        if signed is None:
            raise ExecRouteUnavailable(
                HTTPStatus.NOT_FOUND, {"error": "exec route not found", "retryable": False},
            )
        heartbeat = self.heartbeat(signed, refresh)
        if self.fresh(heartbeat):
            return (
                ExecRoute(
                    session_id=session_id,
                    sandbox_id=signed.sandbox_id,
                    node_id=signed.node_id,
                    job_id=signed.job_id,
                    node_url=heartbeat.node_url,
                    created_at=signed.issued_at_iso,
                    updated_at=signed.issued_at_iso,
                ),
                heartbeat,
            )
        # Keep the distinction between a lost owner and a temporarily silent
        # one. Sessions are never redirected to another owner.
        current = self.routing_store.get_sandbox_readonly(signed.sandbox_id)
        if current is None or current.generation != signed.sandbox_generation:
            loss = (
                self.routing_store.get_sandbox_loss(signed.sandbox_id)
                if current is None else None
            )
            if loss is not None and int(loss["generation"]) == signed.sandbox_generation:
                raise _worker_lost(
                    session_id,
                    signed.sandbox_id,
                    signed.sandbox_generation,
                    loss["lost_at"],
                )
            # A deleted or replaced incarnation keeps no sessions.
            raise ExecRouteUnavailable(
                HTTPStatus.NOT_FOUND,
                {"error": "exec route not found", "retryable": False},
            )
        if current.job_id != signed.job_id or current.worker_state == "detached":
            raise _worker_lost(
                session_id,
                signed.sandbox_id,
                signed.sandbox_generation,
                current.updated_at,
            )
        raise _worker_unreachable(signed)
