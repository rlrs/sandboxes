# Model relay

Agents inside sandboxes make model calls through the model relay. The relay is
an OpenAI-compatible HTTP endpoint that queues each call durably in PostgreSQL.
Model workers fetch the calls by long-polling the relay, run inference near the
model, and post the answers back. Neither side needs an inbound port or SSH:
the sandbox and the worker both connect out to the relay.

```text
sandbox agent ──HTTP──▶ relay ◀──long-poll── model worker ──▶ local inference
                          │
                     PostgreSQL (queue, leases, results, park/wake work)
                          │
                       gateway ──▶ node (keep resident, pause, park, wake)
```

The relay also coordinates the sandbox's lifecycle around each call. A
sandbox waiting for a model answer does no work, so the platform may reclaim
its memory during the wait and must have it running again when the answer
arrives. The relay records that work durably and hands it to the gateway, so
a crash of any process cannot lose an answer that was accepted, or leave a
sandbox parked with its answer undelivered.

Production RL training runs every agent turn through the relay, so model-wait
handling is on the training critical path.

## Processes and credentials

The relay runs as `ucloud-sandboxes serve-model-relay --config <deployment.json>`
(systemd unit `ucloud-sandbox-relay.service` on the gateway VM). It listens on
`relay_port` (default 8092), and a TLS ingress exposes it publicly for model
workers outside the private network. Several relay processes may share one
PostgreSQL schema. See [deployment](deployment.md) for installation.

The relay requires `relay_postgres` in the deployment config and refuses to
start without it.

| Credential | Held by | Grants |
| --- | --- | --- |
| Worker bearer token (`<data_root>/relay-worker-token`) | Model workers, trainer | Register rollouts; poll, renew, respond and report errors; stats; resource-phase advice. |
| Sandbox bearer token (`<data_root>/relay-sandbox-token`) | Sandbox agents using the OpenAI paths | Submit model calls for a named rollout. |
| Registration capability (in the tunnel URL) | One sandbox generation | Submit calls for its own rollout registration only. It is void once the rollout is re-registered or unregistered. |
| Gateway bearer token | The relay process | Ask the gateway to park and wake sandboxes. |

Network reachability grants nothing. Sandbox workloads are untrusted members
of the private network, so every relay endpoint checks a credential. Tokens are
accepted as `Authorization: Bearer <token>` or in the `X-UCloud-Relay-Token`
header. Calls through the capability URL leave `Authorization` untouched, so a
harness can pass its own upstream credential through.

## Calls from the sandbox

### OpenAI-compatible paths

For an unmodified OpenAI client, put the rollout id in the base URL:

```bash
export OPENAI_BASE_URL="https://relay.example.org/rollouts/run-001/v1"
export OPENAI_API_KEY="<sandbox-relay-token>"
```

The relay serves `POST /rollouts/{rollout_id}/v1/chat/completions` and
`POST /rollouts/{rollout_id}/v1/responses`. Streaming is not implemented: a
body with `"stream": true` gets HTTP 400 with error code
`relay_streaming_unsupported`. Errors use the OpenAI error shape.

### Generic HTTP tunnel

The relay can also front any worker-local HTTP service. Registering a rollout
returns a capability URL:

```text
https://relay.example.org/tunnels/<rollout-id>/_relay/<registration-token>/<upstream-path>
```

Requests through it reach the worker with the method, raw percent-encoded
path, query string, safe end-to-end headers and body preserved. The path
`/tunnels/<rollout-id>/<upstream-path>` without the capability also works, but
requires the sandbox bearer token.

The tunnel is buffered HTTP. Request and response bodies are limited to 32 MiB
each, and hop-by-hop headers are removed. Streaming, SSE delivery as it is
produced, trailers and raw TCP are not supported; an SSE body arrives as one
buffered body.

### Retries and idempotency

A disconnected caller does not cancel accepted work. When the agent retries
the same call, the retry reattaches to the original request and receives its
answer without a second inference. Two ways identify "the same call":

- An explicit `X-UCloud-Relay-Request-Id` header (a stable attempt id).
- Without the header, a fingerprint of the request. It becomes reattachable
  only after the original caller disconnects or the relay restarts, so two
  identical calls made on purpose remain two calls.

### Starting the agent

Start the sandbox's long-lived agent as a managed process (SDK
`start_agent()`), not with an attached exec. A managed process keeps its
status and bounded logs across park, wake and cross-node restore. An attached
exec cannot survive a gVisor restore, so a sandbox with one refuses to park
(HTTP 409). See [Linux environments](linux-environments.md) for the
managed-process contract.

The node's generic idle-park timer skips managed-process sandboxes. Their park
points come only from the relay's model-wait protocol below, because an agent
with no inbound requests may still be busy.

To restrict a sandbox's network to the relay alone, configure a named relay in
`sandbox.network_relays` and use `SandboxNetworkPolicy.relay_only()`; see
[security](security.md).

## Rollout registration

A worker registers a rollout before its sandbox makes calls:

```bash
curl -sS -X POST https://relay.example.org/v1/relay/rollouts \
  -H "Authorization: Bearer $WORKER_TOKEN" -H "Content-Type: application/json" \
  -d '{"rollout_id":"run-001"}'
```

The response contains a random 32-hex-digit `registration_token` and the
tunnel capability URL. Registering the same rollout id again creates a new
incarnation: it cancels the old incarnation's work and fences every later call
that carries the old token. Use one registration per sandbox generation.

`GET /v1/relay/rollouts` lists registrations. `DELETE /v1/relay/rollouts/{id}`
with the `registration_token` in the body unregisters a rollout and fails its
pending calls with an OpenAI-shaped error.

### Binding a rollout to a managed sandbox

Park and wake coordination applies only to a rollout bound to a managed
sandbox generation. The SDK's `register_agent_rollout()` takes the managed
sandbox handle and sends reserved metadata:

```json
{
  "rollout_id": "vf-run-001-sandbox-007",
  "metadata": {
    "_ucloud_agent_lifecycle": "managed-process-v1",
    "sandbox_id": "sandbox-007",
    "sandbox_generation": 3
  }
}
```

The relay rejects `sandbox_id` without `sandbox_generation`, and rejects a
sandbox binding without the `managed-process-v1` marker. The generic
`register_rollout()` cannot prove the sandbox runs in managed-process mode, so
it cannot bind one.

## Worker protocol

All worker endpoints take the worker bearer token. Every call after
registration echoes the `registration_token`.

| Endpoint | Purpose |
| --- | --- |
| `GET /worker/poll` | Long-poll for work. Query: `rollout_id`, `registration_token`, `worker_id`, `timeout_seconds` (default 30), `limit` (1–256, default 1), `lease_seconds` (default `relay_worker_lease_seconds`). Returns `{"requests": [...]}`, empty on timeout. |
| `POST /worker/renew` | Extend a lease while inference runs. Body: `registration_token`, `request_id`, `lease_id`, `worker_id`, `lease_seconds`. |
| `POST /worker/respond` | Commit the answer. Body: `registration_token`, `request_id`, `lease_id`, `body`. |
| `POST /worker/error` | Report a failure. Body adds `error`, optional HTTP `status` and optional `retryable`. |
| `POST /worker/heartbeat` | Optional liveness record with `worker_id` and `metadata`, shown in stats. |
| `GET /v1/relay/stats` | Queue, lease, lifecycle, storage and database-pool state. |

A polled request envelope carries `request_id`, `rollout_id`,
`registration_token`, `lease_id`, `lease_expires_at`, `leased_by`,
`delivery_count`, `endpoint`, `method`, `headers` and a tagged `body`. JSON
bodies use `{"encoding":"json","value":...}`; other bytes use
`{"encoding":"base64","value":"..."}`. Responses on `/worker/respond` use the
same encoding.

### Leases and retries

- A poll leases each returned request to the worker for `lease_seconds`. For
  long inference, poll with a lease of about 10 minutes and renew every minute
  or two. A dead worker's request is then retried promptly, without the lease
  having to cover the worst-case generation time.
- An expired lease returns the request to the queue. A response under an
  expired or superseded lease is rejected with 409, even if no other worker
  has polled the request yet.
- A worker error with status 408, 425, 429, 500, 502, 503 or 504, or a
  connection-level message such as "connection reset" or "timed out", releases
  the lease for another delivery, up to three deliveries in total. `status`
  defaults to 502, so an error report without one is retried. An explicit
  `retryable` boolean overrides the classification. Other errors are terminal.
- When the database pool is saturated, poll, respond, renew and unregister
  return 503 with `error_code: relay_database_busy` and `Retry-After: 1`.
  Retry them as-is.

Worker execution is at least once. After a lease expires, a second worker may
start the same request while the first is still computing. Lease ids stop both
results from committing, but cannot undo the duplicate compute. Treat the
response POST as the commit point.

## Durable acceptance

A successful `/worker/respond` commits the answer and its wake work in one
transaction, then returns a receipt at once:

```json
{"ok": true, "request_id": "7fd...", "duplicate": false, "committed": true, "delivery_status": "pending"}
```

- `pending` means delivery work remains. `released` means the answer is
  eligible for the sandbox, or its caller is definitely gone. Neither is proof
  that the sandbox received the answer or is awake. The value is a snapshot at
  commit time.
- Release the inference slot on acceptance. If useful continuation matters,
  observe the sandbox's own progress.
- After a lost acknowledgment, retry the identical response. While the
  completed request is retained, the relay accepts it with `duplicate: true`
  and the current delivery status, on any relay process. Changed bytes or a
  different lease are rejected.
- A slow wake never turns an accepted answer into an error.
- Terminal `/worker/error` reports return the same receipt. Retryable errors
  return `retried: true`, because they requeue inference instead of committing
  a result.

## Park and wake coordination

For a rollout bound to a managed sandbox, the relay drives lifecycle at two
points. Both are durable `relay_lifecycle` rows in PostgreSQL, claimed and
retried by the relay's dispatchers. No dispatcher holds a database transaction
across a gateway call.

1. **Call accepted.** The relay records a park obligation and posts
   `POST /v1/sandboxes/{id}/park` to the gateway, carrying the request and
   sandbox generation as fences. The gateway records the sandbox's program
   state as `model_wait` and forwards the park to the node. A park is skipped
   if the answer has already committed.
2. **The node decides what a park means.** The node's resident-wait policy
   keeps the sandbox running unless memory or memory-pressure stall says it
   must reclaim, and answers 409 `park_deferred` with `Retry-After`; the
   relay keeps retrying while the wait lasts. Under pressure the node picks
   waits to reclaim: first page-cache reclaim, then (with the pause tier) a
   pause in place, and only then a full checkpoint. A checkpoint drops the
   agent's TCP connection; the agent's retry reattaches after restore. See
   [node admission and memory](node-admission-and-memory.md).
3. **Answer committed.** The relay records a wake obligation and posts
   `POST /v1/sandboxes/{id}/wake`. The gateway moves the program state through
   `ready_to_wake`, `waking` and `acting`. For a resident sandbox the wake is a
   no-op; a parked sandbox resumes on its node or, if needed, moves to another
   node through storage-native migration. The relay request, registration and
   idempotency identity do not change across a move.

Each lifecycle call times out after the request's remaining lifetime, at most
600 seconds per attempt; PostgreSQL owns the retry schedule.

Fencing rules:

- A wake can overtake a delayed park acknowledgment. Gateway checks and a
  durable worker fence stop that late park from undoing the wake. Workers
  must advertise the `relay-wake-fence-v1` capability; the gateway refuses
  the unsafe fallback otherwise.
- Request expiry creates a terminal response and queues the caller's wake.
- If the sandbox's generation is gone (deleted or replaced), the relay
  releases delivery but keeps the committed result.

The program states (`model_wait`, `ready_to_wake`, `waking`, `acting`,
`terminal`) appear in the gateway metrics snapshot. They add no autoscaler
demand; a wake that cannot be placed records ordinary pending wake demand
([scaling policy](scaling-policy.md)).

### Node-local model waits

`sandbox.direct_local_model_waits` (default off; requires
`sandbox.direct_pause_tier`) removes the relay and gateway from the common
case. The relay sends no park, and the node detects waits by itself:

- The agent calls the relay over plaintext HTTP/1.1 on the private network,
  through a `sandbox.network_relays` entry. The relay treats a call from a
  non-loopback peer as node-local; callers through the TLS ingress, which
  proxies from loopback, keep the relay-driven protocol.
- The node logs only the IP and TCP headers of relay flows (nftables NFLOG).
  It never reads payloads. It pauses an owned, parkable, managed sandbox with
  a network lease when the last relay payload went out at least 50 ms ago and
  the sandbox used at most 2 ms of CPU in the last 50 ms. Any lifecycle
  activity skips the pause.
- The answer's first packet thaws the sandbox. A paused gVisor network stack
  holds the packet until then, which adds about 10 ms.
- The relay waits up to 4 seconds for the guest's TCP acknowledgment of the
  whole answer. If it arrives, the relay cancels the delayed wake row and the
  wait costs no lifecycle call. If not, for example because the sandbox was
  checkpointed under pressure, the request becomes reattachable and the wake
  row fires through the gateway at 5 seconds.

Plaintext is required because the node must tell the answer from other
traffic. Over TLS 1.3 the server's session ticket arrives after the first
request and looks like a reply. The SDK client must point at the private relay
address (in verifiers-ucloud, `guest_relay_url`), otherwise waits are never
detected.

### Resource-phase advice

Workers may send expiring scheduling hints about a rollout:

```python
await relay.update_resource_phase(
    rollout_id,
    registration_token=registration_token,
    sequence=7,
    phase="model_wait",
    ttl_seconds=60,
    expected_remaining_wait_seconds=20,
)
```

`RelayWorkerClient` (synchronous) and `AsyncRelayWorkerClient` both expose
the method. Over HTTP it is
`POST /v1/relay/rollouts/{rollout_id}/resource-phase` with the worker token
and body `{"registration_token": ..., "update": {...}}`.

- Phases: `model_wait`, `tool`, `rollout_complete`, `training_pause`,
  `training_resume`. Only `model_wait` may carry
  `expected_remaining_wait_seconds` (0–3600).
- `ttl_seconds` defaults to 60 and must be in (0, 3600].
- `sequence` is a positive 63-bit integer, increasing within one
  registration. Retrying an identical sequence and payload returns the
  original expiry without refreshing it. A lower sequence returns
  `accepted: false`; the same sequence with different content returns 409.
  A new registration starts a new sequence.

Advice is never permission to park, resume, execute or deliver. The park
dispatcher forwards the current hint to workers advertising
`resource-phase-advice-v1`, where it only affects which waits are reclaimed
first, and only once measured transition costs exist. Missing or expired
advice leaves ordinary scheduling unchanged. A failed advisory update must
not fail the rollout.

## PostgreSQL storage

PostgreSQL is the relay's only authority. It stores registrations, inference
leases, idempotency identities, exact response bytes and lifecycle
obligations. The gateway's routing and placement state, provider journals and
worker-local journals are separate authorities.

- **Concurrent relays.** Relay processes claim work with
  `FOR UPDATE SKIP LOCKED`; registration and lease tokens reject stale
  workers. A relay restart drops its TCP connections, and callers reattach to
  their durable requests on any relay.
- **Commit path.** An answer and its wake intent commit atomically.
  Acknowledgments come from the commit result, without a second read. Model
  bodies live apart from mutable state, so lease renewals do not rewrite
  payloads. HTTP waiters share batched delivery reads instead of each
  polling a database connection.
- **Notifications after commit.** `NOTIFY` hints are batched after the
  durable commit. PostgreSQL serializes notifying transactions until commit,
  so notifying inside every result transaction defeated concurrent WAL
  flushing under load. Hints are disposable: periodic durable reads recover
  lost hints, due retries and expired claims.
- **Storage budget.** Before accepting a request, the relay reserves the
  maximum response size (32 MiB plus 64 KiB) against
  `relay_postgres.storage_budget_bytes`. When the budget is exhausted, new
  calls get 429 with `Retry-After`. This bounds disk use; it is not a
  concurrency limit. Results waiting for wake are pinned and never evicted.
- **Retention.** Completed responses and worker diagnostics expire after
  `relay_completed_request_retention_seconds`. Once a request completes, its
  original body is dropped from the stored record. Unfinished delivery
  obligations stay until resolved.
- **Durability.** Commits are synchronous, which protects acknowledged
  results from a database process crash with intact storage. Replication,
  backups and failover are the database deployment's job
  (`scripts/backup_relay_postgres.py` exists for backups). Never disable
  `fsync` or `synchronous_commit`.

### Setup

Install the server wheel with the `postgres` extra (worker bundles do not need
database drivers). Put the DSN in a private file with mode 0600, readable by
the relay's service user; keep credentials out of deployment JSON, shell
arguments and logs. Each relay uses up to `max_connections` pooled
connections plus one `LISTEN` connection; size PostgreSQL's connection limit
for that.

The relay does no DDL at startup and rejects a missing or unknown schema.
Create it explicitly:

```sh
python -m ucloud_sandboxes.shared_control migrate \
  --dsn-file /etc/ucloud-sandboxes/postgres-dsn \
  --deployment-id <deployment-id>

python -m ucloud_sandboxes.shared_control status \
  --dsn-file /etc/ucloud-sandboxes/postgres-dsn \
  --deployment-id <deployment-id>
```

`migrate` creates only `relay_*` tables. `status` is read-only: it prints
relay stats without starting a dispatcher. Both take `--schema` (default
`ucloud_shared`).

The earlier SQLite relay backend is removed. A relay that finds an unmigrated
`model-relay.sqlite3` journal refuses to start; the one-way, offline
`import-idle-relay` command of the same module migrates an idle journal and
fences it so no old release can reopen it.

## Observability

`GET /v1/relay/stats` reports `pending`, `leased`, `inflight` and
`inflight_bytes`, `completed_retained`, per-rollout state, worker heartbeats,
`lifecycle` queue and retries, `reserved_storage_bytes` against
`storage_budget_bytes`, database-pool statistics, average queue, worker and
request timings, and a `counters` object.

`counters` holds process-local counts, reset on restart; a missing key means
zero. They include lifecycle claim calls, rows and empty claims per action,
reconciliations, notification batches and self/peer deliveries, and delivery
query and row counts. Compare deltas to tell useful dispatch from redundant
scans.

Telemetry records `ucloud.platform.postgres.duration` with operation, phase
(`pool_wait`, `transaction`, `commit`, `lock_query`) and status labels. No
metric carries a request id.

## Configuration reference

Deployment config keys (see [deployment](deployment.md) for the full file):

| Key | Default | Meaning |
| --- | --- | --- |
| `relay_port` | 8092 | Relay listen port. |
| `relay_request_timeout_seconds` | 7200 | Lifetime of a model call before it expires with a terminal response. |
| `relay_worker_lease_seconds` | 600 | Default lease for `/worker/poll` when the worker passes none. |
| `relay_completed_request_retention_seconds` | 3600 | How long completed responses stay replayable. |
| `relay_postgres.dsn_file` | required | Absolute path of the private DSN file. |
| `relay_postgres.schema` | `ucloud_shared` | `ucloud_shared` or `ucloud_shared_<suffix>`, at most 63 characters. |
| `relay_postgres.max_connections` | 16 | Pooled connections per relay process (plus one `LISTEN`). |
| `relay_postgres.storage_budget_bytes` | 64 GiB | Response storage reserved ahead of acceptance. |
| `sandbox.network_relays` | `{}` | Named private relay endpoints (`HOST:PORT`) that sandboxes reach by a host-owned virtual address. |
| `sandbox.direct_local_model_waits` | `false` | Node-local model waits (requires `sandbox.direct_pause_tier`). |

Fixed limits:

| Limit | Value |
| --- | --- |
| Request and response body | 32 MiB each |
| Poll batch (`limit`) | 1–256 |
| Default poll timeout | 30 s |
| Deliveries after transient worker errors | 3 |
| Rollout id | `[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}` |

## Testing and load qualification

### Component tests on Linux

A Compose stack runs PostgreSQL on an internal Docker network with no
published port. Its trust authentication is for tests only.

```sh
docker compose -p ucloud-pg-qualification \
  -f tests/linux_shared_control/compose.yaml run --build --rm tests \
  python -m unittest tests.test_postgres_relay tests.test_shared_control_postgres

docker compose -p ucloud-pg-qualification \
  -f tests/linux_shared_control/compose.yaml run --rm tests \
  python scripts/benchmark_relay_http.py --agents 512 [--kill-relay]
```

`benchmark_relay_http.py` starts two relay processes, sends real HTTP requests
through the production app, checks every distinct response body, and checks
that durable work drains. `--kill-relay` kills one relay during the response
burst; clients reattach to the other. Wake is a simulated timer
(`--wake-ms`), so this measures the relay and database, not gVisor restore.

### Live managed-agent load

`scripts/live_relay_load_benchmark.py` drives real SDK managed processes
through call acceptance, parking, answer delivery, restore and tool execution.
Model answers are synthetic, so no model API is needed.

Run it from a driver machine outside the autoscaled fleet, against an idle
deployment; it refuses an occupied fleet. Keep tokens in private files and pin
the image (which must contain Python) by digest:

```sh
.venv/bin/python scripts/live_relay_load_benchmark.py \
  --gateway-url https://GATEWAY --relay-url https://RELAY \
  --sandbox-token-file /private/sandbox-token \
  --relay-worker-token-file /private/relay-worker-token \
  --gateway-token-file /private/gateway-token \
  --image python@sha256:IMAGE_DIGEST \
  --sandboxes 256 --cycles 8 \
  --output /private/relay-load.json
```

- Each agent holds resident memory, dirties pages, rewrites files and does CPU
  work each cycle, then waits for a synthetic answer. Size these with
  `--resident-mb`, `--dirty-mb`, `--files`, `--file-kib`, `--cpu-ms`,
  `--model-seconds` and `--model-jitter` to match the real workload.
  `--sqlite-transactions` and `--sqlite-payload-bytes` add a SQLite WAL
  workload whose open writer and reader snapshot must survive every park.
- `--parking-mode natural` answers as soon as the model is ready;
  `--parking-mode forced` waits until every sandbox is actually parked, which
  is what qualifies restore.
- After each answer the agent verifies its memory digest, files, process
  identity and a subprocess tool result, and the driver confirms with a
  separate SDK exec.
- The run passes only if every cycle and cleanup succeed, health checks pass,
  observed guest continuation p95 is below 0.8 s
  (`--continuation-p95-seconds`) and answer-ready-to-usable-exec p95 is below
  1 s (`--useful-action-p95-seconds`). The first cycle per agent is warmup.
  With `--gateway-token-file`, `slo_passed` also requires fresh heartbeats
  and resource samples from every worker hosting the run.
- `--start-signal-file` pauses after all agents start, so a candidate build
  can be verified on every worker before traffic is released.
- Cleanup removes only the run's `relay-load-...` sandboxes and
  registrations, including after cancellation.

`scripts/analyze_relay_load.py <report> --deployment-config <deployment.json>`
joins a report with retained Tempo spans (`--tempo-url`) and relay timestamps
to split latency into phases. It needs synchronized clocks and reports
negative intervals rather than hiding them.

### Headline numbers

- Two relays on one PostgreSQL host, 512 concurrent calls: durable acceptance
  p95 about 0.44 s; caller-visible responses p95 0.9–1.15 s with a simulated
  600 ms wake. Killing one relay mid-burst raised p95 to 1.7–2.1 s and lost no
  result.
- Training-shaped agents with the node keeping waits resident: the relay
  added a median of 53 ms per model call (p95 0.16 s), and no sandbox parked.
- Node-local waits thaw on the answer in about 9 ms at the median, against
  38 ms for a relay-and-gateway wake.
