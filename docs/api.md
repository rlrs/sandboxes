# API

The gateway is the only public HTTP API. Clients create, use and delete
sandboxes through it; the gateway places each sandbox on a worker and proxies
sandbox traffic to that worker's node agent. See
[Architecture](architecture.md) for how the pieces fit.

This page covers what a caller needs to know. For request and response
schemas, use the typed Python SDK and Inspect AI provider in
<https://github.com/rlrs/ucloud-sandboxes-sdk>. Training clients install one
package, [verifiers-ucloud](https://github.com/rlrs/verifiers-ucloud), at a
release tag; it pins the matching SDK and harness toolkit:

```sh
uv add "verifiers-ucloud @ git+https://github.com/rlrs/verifiers-ucloud@v0.3.8"
```

## Connecting

The SDK reads its endpoints and credentials from the environment:

- `UCLOUD_SANDBOX_URL`: gateway base URL, e.g. `https://sandboxes.example.org`.
- `UCLOUD_SANDBOX_API_TOKEN`: the sandbox API key.
- `UCLOUD_RELAY_URL` (and `UCLOUD_RELAY_WORKER_TOKEN`): the model relay, for
  agents whose model calls go through it; see [Architecture](architecture.md).

```python
from ucloud_sandboxes_sdk import SandboxClient
client = SandboxClient.from_env()
```

**Credentials.** Every route except `/healthz` needs a
credential. Clients get exactly one: the **sandbox API key**, sent as
`X-UCloud-Sandbox-Token: <key>` (a UCloud public link can swallow a standard
`Authorization` header; `Authorization: Bearer` also works). It can call every
route marked SDK below, and gets `403` elsewhere. The deployment has three
other keys that clients never see: the gateway control token (operator,
autoscaler and relay; every gateway route), the heartbeat token (workers
posting heartbeats) and the node-control token (gateway to node agents). See
[Operations](operations.md) for where they live.

Do not put the private registry's hostname in client code: the gateway names
and resolves images.

## Endpoints

All routes are JSON unless noted. "Op" means the gateway control token only.

| Route | Key | Purpose |
|---|---|---|
| **Sandboxes** | | |
| `POST /v1/sandboxes` | SDK | Create a sandbox; idempotent by `id`. |
| `GET /v1/sandboxes` | SDK | List sandboxes (full records, or `?view=status`). |
| `DELETE /v1/sandboxes/<id>` | SDK | Delete a sandbox. |
| `GET /v1/sandboxes/<id>/environment` | SDK | Configured profile, features and warnings. Does not wake. |
| `GET /v1/sandboxes/<id>/ssh` | SDK | SSH connection details, if enabled. |
| `POST /v1/sandboxes/<id>/{park,wake,detach}` | Op | Lifecycle control, used by the relay and autoscaler. |
| `POST`, `DELETE /v1/sandboxes/<id>/migration` | Op | Move a parked sandbox to another worker, or abort. |
| **Groups** | | |
| `POST /v1/sandboxes:batch` | SDK | Create `count` sandboxes of one spec (`<group>-0000`, ...). |
| `GET`, `DELETE /v1/sandboxes:batch/<group>` | SDK | Read or delete every member. |
| **Exec and files** | | |
| `POST /v1/sandboxes/<id>/exec` | SDK | Start an exec session. |
| `GET /v1/exec/<session>[/events]` | SDK | Session state; long-poll output and exit events. |
| `POST /v1/exec/<session>/{stdin,close-stdin,signal}` | SDK | Write stdin, close it, or signal the process. |
| `POST /v1/sandboxes/<id>/jobs` | SDK | Start the managed (parking-aware) job. |
| `GET /v1/sandboxes/<id>/jobs/<job>[/logs/<stream>]` | SDK | Job status; job log bytes. Both long-poll. |
| `POST /v1/sandboxes/<id>/jobs/<job>/signal` | SDK | Signal the job. |
| `GET`, `PUT /v1/sandboxes/<id>/files?path=` | SDK | Download or upload one file (raw bytes). |
| `PUT /v1/sandboxes/<id>/archive?path=` | SDK | Upload a tar archive into a directory. |
| **Images** | | |
| `GET`, `PUT /v1/image-contexts/sha256:<hex>` | SDK | Probe or upload a `tar.gz` build context. |
| `POST /v1/images/build`; `GET /v1/images/builds[/<id>]` | SDK | Build an image; list or poll builds. |
| `GET /v1/images`; `POST /v1/images/pull` | SDK | Image inventory; pre-pull an image onto workers. |
| `POST /v1/image-recipes` | SDK | Register image names and their recipes. |
| `POST /v1/images/ensure` | SDK | Report each name's state and start missing builds. |
| `GET /v1/image-index[/names,/name,/task-ids]` | SDK | Read the image index; task ids a trainer may sample. |
| **Capacity and fleet** | | |
| `GET`, `POST`, `DELETE /v1/capacity/prepare[/<id>]` | SDK | Announce sandbox demand ahead of a burst. |
| `GET`, `POST`, `DELETE /v1/builders/prepare[/<id>]` | SDK | The same for image builders. |
| `GET /v1/nodes`, `/v1/demand`, `/v1/registry`, `/v1/metrics` | Op | Heartbeats, autoscaler demand, registry status, the fleet snapshot. |
| `GET /v1/metrics/prometheus` | Op | The snapshot as `ucloud_fleet_*` gauges (Prometheus text) for VictoriaMetrics. |
| `POST`, `DELETE /v1/nodes/<job>/drain` | Op | Drain a worker (`{"evict": true}` also deletes its sandboxes), or undo it. |
| `GET /v1/creates`; `POST /v1/creates:cancel` | Op | Creates waiting for placement; cancel some (`sandbox_ids`) or `all`. |
| `POST /v1/nodes/heartbeat` | heartbeat | Worker heartbeat. |
| **Other** | | |
| `GET /healthz` | none | Service identity and version. |

The node agent on each worker serves the same per-sandbox routes plus
internal heartbeat, drain, snapshot and migration routes, all behind the
node-control token; clients never call it. The model relay has its own API,
used through `RelayWorkerClient` in the SDK; see
[Architecture](architecture.md).

## Errors and retries

Errors are `{"error": "...", "error_code": "...", "retryable": bool}`.

- **`503` with `Retry-After`** is retryable: repeat the identical request.
  Every mutating route is idempotent for a repeated request, so retrying is
  safe. Images still building or warming, no worker currently free, and a
  create still resolving all answer this way.
- **`409`** is a conflict and not retryable as sent: an `id` reused with a
  different spec, a sandbox being deleted, or a stale `generation`.
- **`410` `node_lost`** means the worker holding the sandbox was lost and the
  sandbox cannot be resumed. Create a new one.

## Sandboxes

**Create.** `POST /v1/sandboxes` takes a spec: `id`, `image`, a `profile`
(`container`, the default and most locked down; `linux_session`; or
`linux_host` for images that expect a root, VM-like host),
optional `command`, `env`, resources (`cpus`, `memory_mb`, `disk_mb`; set
both memory and disk), `network`, `ttl_seconds`, `labels`, `toolkits`, and
flags such as `parkable` and `managed_process`. Unknown fields are rejected.
Every profile runs under gVisor.

The `image` is either a registry reference or a gateway image name; the SDK
sends `X-UCloud-Image-Reference-Kind: registry` or `name` from
`Image.from_registry()` and `Image.from_name()`.

A create returns `201` with the sandbox record. If the image needs a build,
import or warm-up first, or no worker fits right now, it returns a retryable
`503` and the gateway records the demand so the autoscaler adds workers. A
shape no worker could ever hold fails with `sandbox_shape_unschedulable`.

**Idempotency.** The sandbox `id` is the idempotency key. Repeating a create
with the same `id` and spec returns the existing sandbox (`200`), or a
retryable `503` while the first create is still in flight. The same `id` with
a different spec is a `409`. A deleted and recreated `id` gets a new
`generation`.

**Groups.** `POST /v1/sandboxes:batch` with `group_id`, `count` (up to 512),
a `spec` without `id`, and `placement` (`pack`, the default, or `spread`)
creates ordinary sandboxes named `<group_id>-0000` onward. The image is
resolved once. If some members are still pending the answer is a retryable
`503`; repeating it places only those. Members can also be deleted one by one.

**Delete.** `DELETE /v1/sandboxes/<id>` deletes on the worker, then removes the
route. Deleting an absent sandbox returns `{"ok": true, "deleted": false}`.

### Lifecycle and states

A sandbox is `running` on a worker or `parked`. Parking (only for sandboxes
created with `parkable: true`) checkpoints the sandbox and releases its CPU
and memory; detach then publishes the checkpoint and frees the worker
entirely, and a later wake restores it on any worker that fits. The relay
parks a sandbox while its agent waits on the model, and the autoscaler
detaches parked sandboxes before scaling down.

Clients do not park or wake directly. **Any request that touches the sandbox
(exec, files, job start, job logs, signals) wakes a parked sandbox first**,
then proceeds; the call simply takes longer. A wake that moved the sandbox to
another worker sets `X-UCloud-Sandbox-Transport-Reset: true`.

An attached exec session blocks parking. For long-running work in a parkable
sandbox, create it with `managed_process: true` and run the work as its
managed job (the SDK's `start_agent()`).

### Fleet status

Pollers should use the compact view:

```
GET /v1/sandboxes?view=status&id=agent-1&id=agent-2
```

It returns, per sandbox, its `id`, `generation`, `state`, `cached_state`,
`created_at`/`updated_at`, and a `node` block (`node_id`, `fresh`,
`attached`, `active_sandboxes`), with no specs, labels or images. Omit `id` to
read every sandbox; up to 256 ids per request. It is served from the gateway's
durable records, not a live call to each worker (`"cached": true`). If a
sandbox's worker has stopped heartbeating, `state` reads `unknown` while
`cached_state` keeps the last known state.

The full `GET /v1/sandboxes` adds specs, labels and resources.
`?refresh=true` asks every worker live; use it sparingly.

## Exec, jobs and files

**Exec sessions.** `POST /v1/sandboxes/<id>/exec` with `command`, optional
`env`, `working_dir`, `stdin` and `tty` returns a session id. Read output with
`GET /v1/exec/<session>/events?after=<seq>&wait_seconds=<0-30>`: it returns
ordered output and exit events after the given sequence, and holds
the request until there is something new or the wait ends. A short command can
finish within the start response with `?initial_wait_seconds=` (up to 0.05).

**Managed jobs.** A `managed_process` sandbox runs one parking-aware job,
started with `POST /v1/sandboxes/<id>/jobs` (`job_id`, `argv`, optional `env`,
`cwd`, log caps). Status and log reads accept `wait_ms` (up to 25 s) and
answer as soon as something changes, the job ends, or the wait expires. Status
reads never wake a parked sandbox; the gateway answers them from its cache. Log
reads at the end of the stream are answered from the cache too; reading older
bytes wakes the sandbox.

Long-polls (exec events, job reads) do not tie up gateway threads, so holding
one per sandbox across thousands of sandboxes is the intended pattern.

**Files.** `PUT`/`GET /v1/sandboxes/<id>/files?path=<absolute path>` upload or
download raw bytes; `PUT .../archive?path=<dir>` extracts a tar archive.
Proxied bodies are limited to 256 MiB.

## Images

[Images](images.md) explains how images are built, named and placed. In
short:

1. `PUT /v1/image-contexts/sha256:<hex>` uploads a deterministic `tar.gz`
   build context (`GET` first to skip one the gateway already has).
2. `POST /v1/images/build` with an `id` and the context digest. Omit `tag`;
   the gateway picks the registry tag and resolves later creates by `id`.
3. Poll `GET /v1/images/builds/<id>`.

Training clients instead register task images by name with
`POST /v1/image-recipes`, then call `POST /v1/images/ensure` with the names
they are about to use. Ensure returns each name's state (`ready`, `building`,
`queued`, `failed`, `unknown`) and starts missing builds; poll it.
`GET /v1/image-index/task-ids?environment=` lists the task ids a trainer may
sample.

## Prepared capacity

`POST /v1/capacity/prepare` with `count`, resources, `ttl_seconds` (default
900) and optionally `image` and `parkable` tells the autoscaler that demand is
coming, so workers boot before the burst. The `image` is stored with the
demand but not pulled ahead; creates pull it. A unit is consumed when a matching sandbox is
created or when its TTL expires. `/v1/builders/prepare` does the same for
image builders.
