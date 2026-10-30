"""Operator controls: drain a worker, and list or cancel queued creates.

``POST`` and ``DELETE /v1/nodes/<job>/drain``:
A drained worker takes no new work at once (placement reads the controller
label as closed admission) and the autoscaler retires it as it does a failing
host: its parks move away and it stops through the drain handshake once empty.
``{"evict": true}`` also deletes the sandboxes it holds, each through the
gateway's own single-sandbox delete, so the stop does not wait for them.
Running sandboxes are never cut off any other way.

``GET /v1/creates`` lists creates still waiting for placement and
``POST /v1/creates:cancel`` cancels some or all; each waiting POST is answered
410, as a delete of the sandbox answers it.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
import re
from typing import Any, Protocol
from urllib.parse import unquote

from ..models import is_operator_drained, utc_now
from .groups import MEMBER_DELETE_CONCURRENCY, _in_context

_DRAIN_PATH = re.compile(r"^/v1/nodes/([^/]+)/drain$")


def drain_job_id_from_path(path: str) -> str | None:
    match = _DRAIN_PATH.match(path)
    return unquote(match.group(1)) if match else None


class DrainExchange(Protocol):
    def _loopback_delete(self, sandbox_id: str) -> tuple[int, dict[str, Any]]: ...


class OperatorDrains:
    def __init__(self, store: Any, routing_store: Any) -> None:
        self.store, self.routing_store = store, routing_store

    def drain(self, ex: DrainExchange, job_id: str, *, evict: bool) -> tuple[int, dict[str, Any], dict[str, str]]:
        heartbeat = self.store.get_heartbeat(job_id, include_inventory=False)
        if heartbeat is None:
            return HTTPStatus.NOT_FOUND, {"error": "node not found", "job_id": job_id}, {}
        if not is_operator_drained(heartbeat):
            self.store.set_operator_drain(job_id, utc_now().isoformat())
        payload: dict[str, Any] = {"job_id": job_id, "draining": True}
        if not evict:
            return HTTPStatus.OK, payload, {}
        routes = self.routing_store.sandbox_routes_matching_node_identity(
            node_id=heartbeat.node_id, job_id=job_id, node_url=heartbeat.node_url or "")
        sandbox_ids = sorted({route.sandbox_id for route in routes})
        with ThreadPoolExecutor(MEMBER_DELETE_CONCURRENCY) as pool:
            answers = list(pool.map(_in_context(ex._loopback_delete), sandbox_ids))
        payload["evicted"] = [
            {"id": sandbox_id, "status_code": status, **({"error": body.get("error")} if status >= 300 else {})}
            for sandbox_id, (status, body) in zip(sandbox_ids, answers)
        ]
        if any(item["status_code"] >= 300 and item["status_code"] != HTTPStatus.NOT_FOUND
               for item in payload["evicted"]):
            return HTTPStatus.SERVICE_UNAVAILABLE, {**payload, "retryable": True}, {
                "Retry-After": "1", "X-UCloud-Sandbox-Retryable": "true"}
        return HTTPStatus.OK, payload, {}

    def undrain(self, job_id: str) -> tuple[int, dict[str, Any]]:
        if self.store.get_heartbeat(job_id, include_inventory=False) is None:
            return HTTPStatus.NOT_FOUND, {"error": "node not found", "job_id": job_id}
        self.store.clear_operator_drain(job_id)
        return HTTPStatus.OK, {"job_id": job_id, "draining": False}


CREATES_PATH, CREATES_CANCEL_PATH = "/v1/creates", "/v1/creates:cancel"


class QueuedCreates:
    def __init__(self, routing_store: Any) -> None:
        self.routing_store = routing_store

    def list(self) -> dict[str, Any]:
        now = utc_now()
        return {"creates": [
            {"sandbox_id": row["sandbox_id"], "state": row["state"], "attempts": row["attempts"],
             "created_at": row["created_at"].isoformat(), "deadline": row["deadline"].isoformat(),
             "age_seconds": max(0, int((now - row["created_at"]).total_seconds()))}
            for row in self.routing_store.open_create_commands_readonly()
        ]}

    def cancel(self, request: object) -> tuple[int, dict[str, Any]]:
        if request == {"all": True}:
            sandbox_ids = [row["sandbox_id"] for row in self.routing_store.open_create_commands_readonly()]
        elif (isinstance(request, dict) and set(request) == {"sandbox_ids"}
              and isinstance(request["sandbox_ids"], list)
              and all(isinstance(item, str) and item for item in request["sandbox_ids"])):
            sandbox_ids = request["sandbox_ids"]
        else:
            return HTTPStatus.BAD_REQUEST, {"error": 'the body is {"all": true} or {"sandbox_ids": [<id>, ...]}'}
        cancelled = [sandbox_id for sandbox_id in dict.fromkeys(sandbox_ids)
                     if self.routing_store.cancel_create_commands(sandbox_id)]
        return HTTPStatus.OK, {"cancelled": cancelled}
