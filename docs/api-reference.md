# API reference

The gateway (the `serve-control-plane` process) is the only public HTTP API.
Clients create, use and delete sandboxes through it; the gateway places each
sandbox on a worker and proxies sandbox traffic to that worker's node agent.
The node agent API is internal and is summarised at the end of this page.

The route table lives in `ControlPlaneHandler` in
[`control_plane.py`](../ucloud_sandboxes/control_plane.py). Which routes the
public SDK key may call is decided by
[`gateway/auth.py`](../ucloud_sandboxes/gateway/auth.py), and the per-sandbox
routes shared by gateway and worker are parsed by
[`http_contract.py`](../ucloud_sandboxes/http_contract.py).

## Clients

The Python SDK and the Inspect AI provider live in a separate repository,
<https://github.com/rlrs/ucloud-sandboxes-sdk>. Client usage, install
instructions and SDK examples belong there; this page documents the HTTP
contract the SDK speaks.

Training clients install one package,
[verifiers-ucloud](https://github.com/rlrs/verifiers-ucloud), at a release tag.
It pins the matching SDK and harness toolkit:

```sh
uv add "verifiers-ucloud @ git+https://github.com/rlrs/verifiers-ucloud@v0.3.8"
```

The SDK reads its endpoints and credentials from the environment:

| Variable | Read by | Meaning |
|---|---|---|
| `UCLOUD_SANDBOX_URL` | `SandboxClient.from_env()` | Gateway base URL, e.g. `https://sandboxes.example.org`. |
| `UCLOUD_SANDBOX_API_TOKEN` | `SandboxClient.from_env()` | The sandbox API key. Sent as `X-UCloud-Sandbox-Token`. |
| `UCLOUD_RELAY_URL` | `RelayWorkerClient.from_env()` | Model relay base URL; see [Model relay](model-relay.md). |
| `UCLOUD_RELAY_WORKER_TOKEN` | `RelayWorkerClient.from_env()` | Relay worker bearer token. |
| `UCLOUD_SANDBOX_RELAY` | Inspect AI provider | Selects `network_policy` relay-only egress to the named relay; see [Security](security.md#network-policy). |

Give SDK users the gateway URL and the sandbox API key only. Never give them
the gateway control token, and never embed the deployment's private registry
hostname or port in client code: the gateway names and resolves images.

## Authentication

Every route except `/healthz` and the dashboard's static assets requires a
credential. A deployment generates four independent tokens; their roles and
the trust model are described in [Security](security.md#credentials).

| Credential | State file | Accepted header | Routes |
|---|---|---|---|
| Sandbox API key | `sandbox-api-token` | `X-UCloud-Sandbox-Token: <key>` or `Authorization: Bearer <key>` | The SDK routes marked **SDK** in the [endpoint index](#endpoint-index). |
| Gateway control token | `gateway-token` | Either header above | Every gateway route. Operator, autoscaler and model relay only. |
| Heartbeat token | `heartbeat-token` | `Authorization: Bearer` only | `POST /v1/nodes/heartbeat` only. |
| Node-control token | `node-control-token` | `Authorization: Bearer` only | Node agent routes. Never valid at the gateway. |

The SDK sends `X-UCloud-Sandbox-Token` because a UCloud public link can consume
a standard `Authorization` header before the request reaches the gateway.

A sandbox API key on a route outside the SDK set returns `403` with
`"sandbox API key is not authorized for this endpoint"`. A missing or unknown
credential returns `401` with `WWW-Authenticate: Bearer`.

## Conventions

Requests and responses are JSON unless stated otherwise. Errors have the
shape:

```json
{"error": "human-readable message", "error_code": "machine_code", "retryable": true}
```

`error_code` and `retryable` are present where the gateway knows them.

- **Retryable errors** are `503` with `Retry-After` (seconds) and
  `X-UCloud-Sandbox-Retryable: true`. Repeat the identical request; every
  mutating route is idempotent for a repeated request.
- **Conflicts** (`409`) are not retryable as sent. Examples: reusing a sandbox
  id with a different spec, `sandbox_delete_pending`, or a lifecycle request
  whose `generation` no longer owns the route.
- **Lost sandboxes.** When the worker holding a sandbox was lost and the
  incarnation cannot resume, sandbox routes return `410` with
  `error_code: "node_lost"`, the sandbox's `sandbox_generation`, `reason` and
  `lost_at`.
- **Upstream pages.** When a worker or proxy answers with a non-JSON error, such
  as an HTML `503` page, the gateway returns JSON with the original status,
  `retryable`, the upstream content type and a short body preview.
- **Body limits.** Proxied sandbox requests are limited to 256 MiB. Uploads
  larger than one 64 KiB transfer chunk stream to the worker as they arrive.

## Endpoint index

**SDK** means the sandbox API key may call the route. Every route is also open
to the gateway control token.

| Method and path | Key | Purpose |
|---|---|---|
| `GET /healthz` | none | Service identity and version. |
| `GET /`, `GET /dashboard` and its assets | none | Operator dashboard page; its data comes from `/v1/metrics`. |
| `POST /v1/sandboxes` | SDK | [Create a sandbox](#create-a-sandbox). |
| `GET /v1/sandboxes` | SDK | [List sandboxes](#list-sandboxes) (full or `view=status`). |
| `DELETE /v1/sandboxes/<id>` | SDK | [Delete a sandbox](#delete-a-sandbox). |
| `GET /v1/sandboxes/<id>/environment` | SDK | [Describe the configured environment](#environment-description). |
| `POST /v1/sandboxes:batch` | SDK | [Create a sandbox group](#sandbox-groups). |
| `GET`, `DELETE /v1/sandboxes:batch/<group>` | SDK | Read or delete a sandbox group. |
| `GET`, `PUT /v1/sandboxes/<id>/files?path=` | SDK | [Download or upload one file](#files). |
| `PUT /v1/sandboxes/<id>/archive?path=` | SDK | Upload a tar archive into a directory. |
| `GET /v1/sandboxes/<id>/ssh` | SDK | SSH connection details for an SSH-enabled sandbox. |
| `POST /v1/sandboxes/<id>/exec` | SDK | [Start an exec session](#exec-sessions). |
| `GET /v1/exec/<session>` | SDK | Exec session state. |
| `GET /v1/exec/<session>/events` | SDK | Long-poll exec output and exit events. |
| `POST /v1/exec/<session>/stdin` | SDK | Write to stdin. |
| `POST /v1/exec/<session>/close-stdin` | SDK | Close stdin. |
| `POST /v1/exec/<session>/signal` | SDK | Signal the process. |
| `POST /v1/sandboxes/<id>/jobs` | SDK | [Start the managed job](#managed-jobs). |
| `GET /v1/sandboxes/<id>/jobs/<job>` | SDK | Job status. |
| `GET /v1/sandboxes/<id>/jobs/<job>/logs/<stdout\|stderr>` | SDK | Job log bytes. |
| `POST /v1/sandboxes/<id>/jobs/<job>/signal` | SDK | Signal the job. |
| `POST /v1/sandboxes/<id>/park` | operator | [Park](#park-wake-detach-and-migration). |
| `POST /v1/sandboxes/<id>/wake` | operator | Wake. |
| `POST /v1/sandboxes/<id>/detach` | operator | Publish a parked sandbox and release its worker. |
| `POST /v1/sandboxes/<id>/migration` | operator | Move a parked sandbox to another worker. |
| `DELETE /v1/sandboxes/<id>/migration?migration_id=` | operator | Abort a migration before its route switch. |
| `GET`, `PUT /v1/image-contexts/sha256:<hex>` | SDK | [Probe or upload a build context](#build-contexts-and-builds). |
| `POST /v1/images/build` | SDK | Build an image. |
| `GET /v1/images/builds` | SDK | List builds across builders. |
| `GET /v1/images/builds/<id>` | SDK | One build's status. |
| `GET /v1/images` | SDK | Gateway image inventory. |
| `POST /v1/images/pull` | SDK | Pre-pull an image onto workers. |
| `POST /v1/image-recipes` | SDK | [Register image names and recipes](#image-recipes-and-the-image-index). |
| `POST /v1/images/ensure` | SDK | Report and start builds for registered names. |
| `GET /v1/image-index`, `/names`, `/name`, `/task-ids` | SDK | Read the image index. |
| `GET`, `POST /v1/capacity/prepare` | SDK | [List or publish prepared sandbox capacity](#prepared-capacity). |
| `DELETE /v1/capacity/prepare/<id>` | SDK | Withdraw prepared capacity. |
| `GET`, `POST /v1/builders/prepare` | SDK | List or publish prepared builder capacity. |
| `DELETE /v1/builders/prepare/<id>` | SDK | Withdraw prepared builder capacity. |
| `GET /v1/nodes` | operator | [Stored heartbeats](#operator-reads). |
| `GET /v1/demand` | operator | Pending and prepared demand, as the autoscaler sees it. |
| `GET /v1/metrics` | operator | Dashboard snapshot. |
| `GET /v1/registry` | operator | Registry status. |
| `POST /v1/nodes/heartbeat` | heartbeat token | [Worker heartbeat](#heartbeats). |

## Sandboxes

### Create a sandbox

`POST /v1/sandboxes` takes a sandbox spec:

```json
{
  "id": "demo-1",
  "image": "python:3.12-slim",
  "profile": "container",
  "command": ["sleep", "infinity"],
  "env": {"REQUEST_ID": "req-1"},
  "cpus": 1,
  "memory_mb": 2048,
  "disk_mb": 4096,
  "network": "bridge",
  "ttl_seconds": 600,
  "labels": {"tenant": "example"}
}
```

| Field | Default | Meaning |
|---|---|---|
| `id` | required | 1 to 64 letters, digits, `_`, `.` or `-`, starting with a letter or digit. The idempotency key. |
| `image` | required | Registry reference or gateway image name; see [`X-UCloud-Image-Reference-Kind`](#image-references). |
| `profile` | `container` | `container`, `linux_session` or `linux_host`; see [Linux environments](linux-environments.md). |
| `command` | image default | Argument vector. Must be empty for a `managed_process` sandbox. |
| `env`, `working_dir` | none | Process environment and working directory. |
| `cpus`, `memory_mb`, `disk_mb` | none | Resource request. The gateway requires at least one. Workers require explicit `memory_mb` and `disk_mb`, so set both. |
| `network` | `bridge` | `bridge` or `none`. Must match the worker's node-wide network mode; see [Security](security.md#network-isolation). |
| `network_policy` | `{"egress": "direct"}` | Egress policy: `direct`, or `{"egress": "relay", "relay": "<name>"}`. See [Security](security.md#network-policy). |
| `dns_servers` | none | Up to three IPv4 resolvers. Requires `bridge`; not allowed with relay egress. |
| `ttl_seconds` | none | Lifetime bound. |
| `parkable` | `false` | The sandbox may be parked. Requires `memory_mb` and `disk_mb`; forbids SSH. |
| `managed_process` | `false` | The sandbox runs one parking-aware [managed job](#managed-jobs). Requires `parkable`, the `container` profile and a writable rootfs. |
| `ssh` | disabled | `enabled`, `user`, `host`, `host_port`, `container_port`, `authorized_keys`. Requires `bridge`. |
| `security` | per profile | `user`, `cap_drop`, `cap_add`, `supplementary_groups`, `no_new_privileges`, `pids_limit`, `read_only_rootfs`, `init`. Partial overrides inherit the profile defaults. |
| `filesystem` | per profile | `workspace_path`, `tmpfs_mb`, `run_tmpfs_mb`, `shm_mb`, `workspace_storage`, `management_helper`, `enforce_disk_quota`. |
| `linux_host` | per profile | `enable_cron`, `enable_sshd`, `keep_alive`, `writable_paths`. |
| `required_features` | none | Environment features the sandbox must have; see [Linux environments](linux-environments.md). |
| `toolkits` | none | Up to four `name:tag` toolkit layers stacked on the image; the gateway pins each to a digest. |
| `labels` | none | String map. Keys starting with the reserved `ucloud-sandboxes.` prefix are rejected. |

Unknown fields are rejected. `environment_root` is set by the gateway, never
by clients.

A successful create returns `201` with the sandbox record. Images that need a
build, import or warm-up first return a retryable `503` (for example
`image_building`, `image_import_pending`, `image_warmup_pending`) until the
image is ready. A shape larger than any worker can hold returns
`sandbox_shape_unschedulable`. When no worker currently fits, the request is
recorded as pending demand for the autoscaler and answered with a retryable
`503`.

**Idempotency.** The sandbox `id` is the idempotency key; there is no separate
header. Repeating a create with the same `id` and the same normalised spec
returns the existing sandbox (`200`), or a retryable `503` while the original
create is still unresolved. Reusing an `id` with a different image, resources,
command, environment, security, filesystem or labels is a `409` conflict.

**Profiles.** `container` keeps the hardened defaults (UID/GID 1000, all
capabilities dropped, `no_new_privileges`, 256 PIDs). `linux_session` keeps the
same restrictions but starts a persistent shell with the workspace as cwd.
`linux_host` uses root-oriented defaults for benchmark images that expect a
VM-like host, and can start cron or sshd if the image already has them. Every
profile runs under gVisor; none is a booted VM. The full contract is in
[Linux environments](linux-environments.md).

**SSH.** SSH-enabled sandboxes bind SSH to the worker's localhost
(`ssh.host` defaults to `127.0.0.1`). Reach it through the gateway or a tunnel
rather than exposing container ports.

### Sandbox groups

`POST /v1/sandboxes:batch` creates `count` ordinary sandboxes of one spec,
named `<group_id>-0000`, `<group_id>-0001`, and so on:

```json
{"group_id": "rollout-7", "count": 64, "spec": {"image": "...", "cpus": 1, "memory_mb": 2048, "disk_mb": 4096}, "placement": "pack"}
```

- `group_id` is 1 to 59 characters of `[A-Za-z0-9_.-]`, starting
  alphanumeric. `count` is 1 to 512. `spec` is a sandbox spec without `id`.
- `placement` is `pack` (default: fill few workers so each attaches the image
  once) or `spread`.
- The image is resolved once for the whole group. Members a worker rejects are
  re-planned elsewhere for up to four rounds.
- The response lists each member with its `status`, `generation` and
  `node_id`, plus `counts` per status. `201` means members were created; `200`
  means all already existed. If some members are still `pending` or
  `creating`, the answer is a retryable `503`; repeating the request places
  only those. Any `conflict` member makes it `409`; a failed member makes it
  `502`.

`GET /v1/sandboxes:batch/<group>` returns the members' current states.
`DELETE` closes the group to further placement and deletes every member
through the ordinary delete path; if any member delete is still in progress
it returns a retryable `503`. Members are ordinary sandboxes and can also be
deleted one by one.

### List sandboxes

`GET /v1/sandboxes` returns every sandbox route with its full nested spec,
labels, resources and snapshot descriptors:

```json
{
  "sandboxes": [
    {
      "id": "demo-1",
      "sandbox_id": "demo-1",
      "image": "python:3.12-slim",
      "labels": {"tenant": "example"},
      "spec": {"id": "demo-1", "image": "python:3.12-slim"},
      "state": "running",
      "cached_state": "running"
    }
  ],
  "cached": true
}
```

`cached: true` means the response is built from the gateway's durable route
and heartbeat records rather than a live call to each worker. Every request
reads the current records; there is no response TTL, and only identical
requests that overlap in time share one read. Records carry `cached_state`,
route timestamps and node freshness, so a client can tell a fresh running
route from one whose worker has gone stale (`state` is then `unknown` while
`cached_state` keeps the last durable worker state).

`GET /v1/sandboxes?refresh=true` instead asks every worker, reconciles their
answers into the route table, and returns `"cached": false`. Use it only when
a live fan-out is intended.

**Compact status view.** Clients that poll lifecycle or placement should use
`GET /v1/sandboxes?view=status`, which omits specs, labels, resources, images
and snapshot descriptors:

```json
{
  "sandboxes": [{
    "id": "agent-1",
    "spec": {"id": "agent-1"},
    "generation": 2,
    "state": "parked",
    "cached_state": "parked",
    "node": {
      "node_id": "10.42.0.5",
      "job_id": "worker-job",
      "node_url": "http://10.42.0.5:8090",
      "active_sandboxes": 0,
      "fresh": true,
      "attached": true
    },
    "created_at": "2026-09-28T12:00:00+00:00",
    "updated_at": "2026-09-28T12:01:00+00:00"
  }],
  "cached": true,
  "refresh_supported": false,
  "view": "status"
}
```

`generation` identifies the sandbox incarnation; a deleted and recreated id
gets a new generation. Freshness, quarantine and snapshot validation follow
the same rules as the full view.

Repeat `id` to read only some sandboxes:
`GET /v1/sandboxes?view=status&id=agent-1&id=agent-2`. IDs are exact matches,
returned sorted; duplicates collapse and unknown IDs are absent. The filter
accepts at most 256 nonempty IDs of at most 512 characters each, with no NUL,
and at most 256 KiB encoded. `id` without `view=status`, an unknown or
repeated `view`, and `view=status&refresh=true` return `400`.

### Delete a sandbox

`DELETE /v1/sandboxes/<id>` deletes the sandbox on its worker and then removes
the route. Deleting an absent sandbox returns `{"ok": true, "deleted": false}`.
Once a delete has started, other requests for the sandbox return `409`
`sandbox_delete_pending`. A detached or migrating sandbox is resolved before
the delete is forwarded.

### Environment description

`GET /v1/sandboxes/<id>/environment` returns `{"environment": ...}`: the
configured profile, features and warnings derived from the spec. It does not
wake the sandbox and does not expose environment values. See
[Linux environments](linux-environments.md).

## Files

| Route | Body |
|---|---|
| `PUT /v1/sandboxes/<id>/files?path=<absolute path>` | Raw file bytes. |
| `GET /v1/sandboxes/<id>/files?path=<absolute path>` | Returns raw file bytes. |
| `PUT /v1/sandboxes/<id>/archive?path=<absolute directory>` | A tar archive, extracted into the directory. |

File routes wake a parked sandbox. A worker that predates archive upload
answers `501` `archive_upload_unsupported`; the SDK then uploads the files one
by one.

## Exec sessions

Exec is session-based. `POST /v1/sandboxes/<id>/exec` takes exactly these
fields:

```json
{"command": ["sh", "-lc", "echo ok"], "env": {}, "working_dir": null, "stdin": false, "tty": false}
```

The optional query `initial_wait_seconds` (0 to 0.05) lets a short command's
output and exit arrive in the start response, saving a poll; it is ignored for
`stdin` or `tty` sessions. The response carries the session,
whose id routes all later calls.

| Route | Parameters |
|---|---|
| `GET /v1/exec/<session>` | Session state: `status`, `exit_code`, `stdin_open`, `final_sequence`. |
| `GET /v1/exec/<session>/events` | `after` (sequence, default 0), `limit` (default 100), `wait_seconds` (0 to 30). Returns `session` and ordered `events` (`sequence`, `stream`, `data`, `exit_code`, `created_at`). |
| `POST /v1/exec/<session>/stdin` | `{"data": "...", "eof": false}` |
| `POST /v1/exec/<session>/close-stdin` | none |
| `POST /v1/exec/<session>/signal` | `{"signal": 15}`, an integer in 1 to 64. Signalling a finished session is a no-op. |

The worker names a session with a gateway-signed prefix that binds the sandbox
incarnation and worker, so the gateway routes later calls without a database
read. If that worker is gone, the session reads as lost; it is never
redirected.

An attached exec session blocks parking, because its host-side transport
cannot be reattached after a gVisor restore. Run long-lived, parking-aware
work as a managed job instead (the SDK's `start_agent()`).

## Managed jobs

A `managed_process` sandbox runs one parking-aware primary process, its job.

`POST /v1/sandboxes/<id>/jobs` starts it:

```json
{"job_id": "agent", "argv": ["python", "/app/agent.py"], "env": {}, "cwd": "/workspace", "max_stdout_bytes": 1048576, "max_stderr_bytes": 1048576}
```

`job_id` and `argv` are required. The response is `201` with `{"job": <record>}`.
A second start for a generation that already has its primary returns `409`
`primary_already_owned` with the existing `job_id`.

| Route | Parameters | Wakes a parked sandbox |
|---|---|---|
| `POST /v1/sandboxes/<id>/jobs` | start spec | yes |
| `GET /v1/sandboxes/<id>/jobs/<job>` | `wait_ms`, `after` | no; served from the gateway's cache |
| `GET /v1/sandboxes/<id>/jobs/<job>/logs/<stdout\|stderr>` | `offset`, `limit` (1 to 1 MiB), `wait_ms` | yes, except as below |
| `POST /v1/sandboxes/<id>/jobs/<job>/signal` | `{"signal": 15}` | yes |

- **Status** returns `{"job": <record>}`. The record carries
  `"cursor": "<sequence>.<stdout_bytes>.<stderr_bytes>"`.
- **Logs** return `stream`, `offset`, `next_offset`, base64 `data` and `eof`
  (`next_offset` reached the stream's size), plus `"job": <record>`.
- **Waiting reads.** With `wait_ms` (clamped to 0 to 25000), the worker answers
  when the record's cursor differs from `after` (status), when the stream has
  bytes past `offset` (logs), when the job is terminal, or when the wait ends.
  The gateway gives the worker `wait_ms` plus its usual 60 s budget, and a
  waiting read holds no gateway handler thread (see
  [Held reads](#held-reads-and-small-uploads)).
- **The gateway's cache.** The record from every successful status or logs
  response is cached, fenced by sandbox generation and sequence; within one
  sequence the cached byte counts never shrink. Park responses, listings and
  heartbeat inventory refresh the same cache.
- **Parked or transitioning sandboxes** are answered from the cache, with
  `Retry-After: 1` when the read waits:
  - A status read returns the cached record; a parked sandbox with no cached
    record returns `404`.
  - A waiting logs read whose `offset` is at or past the cached byte count for
    its stream returns
    `{"stream", "offset", "next_offset": offset, "data": "", "eof": true, "job"}`
    without waking the sandbox.
  - A logs read below the cached count, one that does not wait, or one with no
    cached record wakes the sandbox, because those bytes exist only inside it.

## Park, wake, detach and migration

These routes take the gateway control token. The model relay and the
autoscaler call them; SDK clients park and wake indirectly, through the relay
and through implicit wake (any route marked "wakes" above wakes a parked
sandbox before it is forwarded).

**Park and wake.** `POST /v1/sandboxes/<id>/park` and `.../wake` take
`operation_id` (required, stable across retries) and `generation` (required
for wake; checked against the current route when given). A generation that no
longer owns the route returns `409`. Relay-driven requests also carry
`request_id` and `rollout_id` together, and only apply to parkable
`managed_process` sandboxes. The response carries
`X-UCloud-Sandbox-Transport-Epoch`, which changes whenever a migration moves
the sandbox, and `X-UCloud-Sandbox-Transport-Reset: true` when a wake moved it
so existing connections must be reopened.

The default park releases compute and seals local storage without putting
snapshot publication on the critical path. Wake reuses the same worker when it
still holds the sandbox and has capacity; otherwise placement picks a worker.
Lifecycle semantics are in [State protocol](state-protocol.md) and
[Node admission and memory](node-admission-and-memory.md).

**Detach.** `POST /v1/sandboxes/<id>/detach` takes `{}` and is the durability
boundary for scale-down. The gateway publishes the parked snapshot if it is
not yet published, validates and records its `storage-native-v1` descriptor
and registry reference, then evicts it from the worker. It is idempotent.
Ambiguous eviction returns a retryable `503` and leaves the route
`detaching`; the gateway never reports the storage as freed without a
successful retry or a fresh complete heartbeat proving it absent. Waking a
detached sandbox imports the snapshot on a worker that fits it. The autoscaler
sends `{"if_cold": <observation>}` to detach only if the route is unchanged
since it decided.

**Migration.** `POST /v1/sandboxes/<id>/migration` moves a parked sandbox
between two workers that both advertise `storage-native-v1` and
`sandbox-migrate-storage-native-v1`. Optional fields: `migration_id` for
idempotent retries and `destination_node_id` to require a specific ready
worker. The response returns the migration journal and the current route. A
retryable `503` keeps the journal phase for the next identical request.
`DELETE /v1/sandboxes/<id>/migration?migration_id=<id>` aborts only before the
atomic route switch; after it, retry `POST` until the source is finalised.

## Images

[Images](images.md) describes how images are built, stored and placed. This
section lists the HTTP surface.

### Image references

`POST /v1/sandboxes`, `POST /v1/capacity/prepare` and `POST /v1/images/pull`
accept the optional `X-UCloud-Image-Reference-Kind` header:

| Value | Meaning |
|---|---|
| `registry` | `image` is a registry reference; no image-name lookup. |
| `name` | `image` is a gateway image name. A retryable `503` (`image_inventory_incomplete`) while the cross-node inventory is incomplete; a non-retryable `image_id_not_found` when a complete inventory has no match. |
| `auto` or absent | Best-effort: try the name, else treat it as a reference. |

The SDK sends `registry` from `Image.from_registry(...)` and `name` from
`Image.from_name(...)`.

### Build contexts and builds

1. Upload the deterministic `tar.gz` build context with
   `PUT /v1/image-contexts/sha256:<hex>`, `Content-Type: application/gzip` and
   `Content-Length`. The gateway verifies the digest and stores the blob
   (`201`, or `200` with `"deduplicated": true` if already present).
   `GET` on the same path returns `{digest, size, deduplicated}` or `404`, so a
   client can skip uploading a context the gateway already holds.
2. `POST /v1/images/build` with `id`, `context_archive_digest`,
   `context_archive_size`, `context_archive_format: "tar.gz"` and optional
   Dockerfile and build arguments. Omit `tag`: the gateway generates the
   private registry tag, forces `push`, and later resolves sandbox creates by
   `id`. An explicit `tag` is still accepted for external registry flows.
3. Poll `GET /v1/images/builds/<id>`, or list with `GET /v1/images/builds`.

The context survives a retry when no builder is ready (`builder_not_ready`),
and is streamed to the chosen builder only if that builder lacks it. When the
registry's disk is under pressure, builds return a retryable `503`.

### Inventory and pull

`GET /v1/images` returns the gateway's image inventory across workers.
`POST /v1/images/pull` takes `image`, optional `id`, `count` (default 1),
resource fields and `sandbox_nodes_only` (default `true`), pulls the image to
up to `count` ready workers, and returns per-node cache hits, pulls and
failures.

### Image recipes and the image index

These routes serve training clients that sample tasks by name. They return
`400` `image_recipes_unavailable` on a deployment without an image index.

- `POST /v1/image-recipes` registers up to 1000 names per request:
  `{"recipes": [...], "partial": false}`. Each recipe names a build context
  (uploaded first) or a `prepared_reference`, plus its environment, tasks and
  source. Without `partial`, one refused name refuses the request; with
  `"partial": true` the rest register and the refused come back with reasons
  (`build_context_missing`, `image_not_prepared`).
- `POST /v1/images/ensure` with `{"names": [...]}` returns each name's state
  (`ready` with its reference, `building`, `queued`, `failed`, `unknown`) and
  starts builds for missing images. It is idempotent; callers poll it.
- `GET /v1/image-index` summarises names per environment and state.
  `/v1/image-index/names?environment=&state=&after=&limit=` pages names
  (default limit 500). `/v1/image-index/name?name=` returns one name.
  `/v1/image-index/task-ids?environment=` returns the task ids a trainer may
  sample.

## Prepared capacity

Prepared capacity tells the autoscaler about demand that is about to arrive,
so workers boot before the burst.

`POST /v1/capacity/prepare` takes `count` (default 1), `cpus`, `memory_mb`,
`disk_mb`, `ttl_seconds` (default 900), and optional `id`, `image` and
`parkable`. With `parkable: true` the gateway expands `disk_mb` into the same
checkpoint reservation a parkable create uses, so each future sandbox claims
its prepared unit exactly. A unit stays until a matching sandbox allocation
claims it or its TTL expires; the provider accepting a VM does not consume it.

With `image`, the gateway also registers an image warm-up with the same id and
TTL. Warm-up runs as workers heartbeat and completes once cached capacity fits
`count` sandboxes. The response then includes `image_warmup` and an
`image_prewarm` scheduling summary.

`GET /v1/capacity/prepare` lists prepared units with the current demand, and
`DELETE /v1/capacity/prepare/<id>` withdraws one.

`/v1/builders/prepare` is the same for image builders: `POST` takes `count`,
`ttl_seconds` (default 900) and optional `id`; `GET` lists; `DELETE .../<id>`
withdraws.

## Operator reads

**`GET /v1/nodes`** returns `{"nodes": [...]}`, the stored heartbeat of every
worker.

**`GET /v1/demand`** returns pending and suppressed pending resources and
counts, prepared and desired resources, the oldest pending age, pending image
builds, prepared and desired builders, and the pending, prepared and warm-up
items behind them.

**`GET /v1/registry`** returns `{"registry": ...}`, the registry status.

**`GET /v1/metrics`** returns the dashboard snapshot. By default it uses a
recent window of 500 events, a cached registry summary, and a 1 s response
cache, so dashboard polling stays cheap. `?full=true` reads up to 10,000
events and rescans the registry; `?refresh_registry=true` only rescans the
registry. Top-level sections:

| Key | Contents |
|---|---|
| `nodes` | Counts (total, fresh, compatible, sandbox, ready, draining, admission closed, builder) and one item per node with resources, load and capabilities. |
| `resources` | Total, used, free and load per role (`fresh`, `sandbox`, `builder`, `schedulable_*`), with measured CPU and memory use. |
| `sandboxes` | Route counts by state, pending and suppressed demand, stale and portable-parked routes. |
| `capacity`, `builders` | Prepared sandbox and builder units. |
| `exec` | Open exec sessions. |
| `programs` | Relay program requests by phase (counts, resources, ages, latency percentiles), never per request. |
| `images` | Pending builds and warm-ups, active and failed builds. |
| `scale_up` | VM scale-up latency samples. |
| `autoscaler` | The latest cycle: actions, reasons, `effective_policy` (every non-secret `ScalePolicy` field; read-only) and `execution` (scale-down planning, drain and detach outcomes, provider operation outcomes). |
| `vm_lifecycle` | Per-VM boot intervals, defined below. |
| `events` | Recent raw events. |
| `telemetry` | OTLP exporter queue and span counters. Traces themselves go to the OTLP backend; see [Operations runbook](operations-runbook.md). |
| `registry` | Registry summary, marked `cached` when it came from the cache. |

The `vm_lifecycle` intervals have narrow meanings:

- `ucloud_created_to_running_ms`: the provider's VM wait, from UCloud
  timestamps.
- `running_to_first_init_attempt_ms`: until the controller starts the first
  SSH bootstrap attempt. It includes SSH start-up and autoscaler polling delay,
  because UCloud exposes no SSH-ready timestamp.
- `last_successful_package_stage_ms` and `last_successful_remote_init_ms`:
  package transfer and the remote init script.
- `first_init_attempt_to_first_heartbeat_ms`: from bootstrap start to node
  registration. The heartbeat can arrive just before the init command exits.
- `first_heartbeat_to_first_sandbox_ms`: to the first sandbox placed on the
  node.

## Heartbeats

Workers publish their state with `POST /v1/nodes/heartbeat`, authenticated by
the heartbeat token. A trimmed example:

```json
{
  "node_id": "10.42.0.5",
  "job_id": "worker-job",
  "updated_at": "2026-09-28T09:43:15+00:00",
  "node_url": "http://10.42.0.5:8090",
  "agent_version": "<package-version>",
  "capabilities": ["sandbox", "image-cache", "storage-native-v1"],
  "active_sandboxes": 1,
  "draining": false,
  "admission_open": true,
  "total_resources": {"vcpu": 32, "memory_mb": 98304, "disk_mb": 2000000},
  "used_resources": {"vcpu": 2, "memory_mb": 4096, "disk_mb": 20000},
  "reserved_resources": {"vcpu": 2, "memory_mb": 4096, "disk_mb": 20000},
  "node_epoch": "<boot id>",
  "inventory": [],
  "inventory_complete": true,
  "labels": {"pool": "default"}
}
```

The full field set is `NodeHeartbeat` in
[`models.py`](../ucloud_sandboxes/models.py); how the gateway uses
heartbeats for freshness, inventory and placement is in
[State protocol](state-protocol.md).

## Health

`GET /healthz` needs no credential and returns the service identity and
package version:

```json
{"ok": true, "service": "control-plane", "version": "<package-version>"}
```

`service` is `control-plane`, `node-agent`, `builder-agent` or `model-relay`.
The gateway answers `503` with `"ok": false` and a `routing_writer` or
`registry_usage` error when its routing writer process or registry usage
accounting has failed.

## Held reads and small uploads

Exec event polls, job reads with `wait_ms`, and small file uploads (up to one
64 KiB transfer chunk) can wait on the worker for a long time. The gateway
does not hold a handler thread for them. The handler authenticates, looks up
the route, checks the generation and performs any implicit wake, then hands
the client socket to one asynchronous owner (`AsyncGatewayResponses` in
[`gateway_response_proxy.py`](../ucloud_sandboxes/gateway_response_proxy.py)).
That owner makes the worker request, writes the response, and cleans up on
completion, cancellation or client disconnect. Lifecycle operations and exec
creation stay on the synchronous path because their responses change durable
state.

- Job reads are the one handoff with work after the worker answers: the
  returned record is cached, as on the synchronous path. That projection runs
  on a worker thread, never on the I/O loop.
- The asynchronous worker client keeps separate connection lanes per worker:
  128 control, 256 event-poll and 1,024 held-read connections. About one job
  read per running managed sandbox is held at a time, so held reads must
  neither queue behind event polls nor starve them.
- A small upload's bytes are charged to the gateway's upload memory budget
  until the response is cleaned up. When that budget is full, the request
  falls back to the synchronous handler; no new rejection is introduced.
- Worker requests on this path are never replayed after a disconnect.

`build_server(async_proxy_responses=False)` selects the synchronous path for
every request. The asynchronous owner is the default; a request is never
retried through the other path.

## Node agent API

The node agent on each worker (`serve-direct-node-agent`; the Rust `noded`
daemon fronts its create, exec and job routes) serves the same per-sandbox
routes the gateway proxies, plus internal routes the gateway calls directly.
Every route except `/healthz` requires the node-control bearer token. Clients
never call it.

| Route | Purpose |
|---|---|
| `GET /healthz` | Identity and version. |
| `GET /v1/heartbeat` | The node's current heartbeat, pulled by the gateway. |
| `POST /v1/drain` | `{"draining": bool, "token": "..."}`: close or reopen admission for scale-down. |
| `GET`, `POST /v1/sandboxes`; `DELETE /v1/sandboxes/<id>` | Inventory (`?sandbox_id=` for one), create, delete. |
| Per-sandbox routes | `environment`, `files`, `archive`, `ssh`, `exec`, `jobs`, `park`, `wake`, as at the gateway. |
| `/v1/exec/<session>` routes | As at the gateway. |
| `POST /v1/sandboxes/<id>/publish-parked`, `/evict-published`, `/snapshot/publish`, `/commit-export` | Snapshot publication and eviction for detach. |
| `POST /v1/sandboxes/<id>/migration/{prepare,activate,finalize,abort,abort-import}`, `POST /v1/migrations/import` | Migration steps. |
| `GET /v1/images`, `GET /v1/images/builds[/<id>]`, `POST /v1/images/build`, `POST /v1/images/pull`, `GET`/`PUT /v1/image-contexts/<digest>` | Image cache and builds (builders serve the build routes). |
