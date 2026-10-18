# Sandbox state protocol

This document is the required contract for durable sandbox operations and for
how the gateway, the autoscaler and a node treat a node that goes silent, is
suspended or powered off, reboots, is deleted by its provider, or is drained.
Every component persists and enforces these properties; there is no reduced or
generation-zero protocol. [Architecture](architecture.md) shows where each
authority lives; [scaling-policy.md](scaling-policy.md) owns the capacity rules.

## Principles

1. **Silence is never loss.** A late heartbeat, a failed probe, or a provider
   suspension or power-off makes a node unschedulable and its traffic
   retryable. It never declares a sandbox lost or terminates an occupied VM.
2. **A new boot loses processes, not the node.** A fresh authenticated
   heartbeat with a new `node_epoch` (the guest kernel's `boot_id`) proves the
   old boot's processes are gone. Running, paused and half-captured
   incarnations are lost; complete local parks survive on disk and are kept.
3. **Loss has one answer:** 410 `node_lost` (exec sessions: 410
   `exec_worker_lost`), never a bare 404.
4. **Termination needs a proof:** a drain acknowledgement of an empty node, or
   an empty, lease-expired node that never heartbeated or failed a direct
   transport probe in the same cycle. No provider status authorizes it.
5. **Elapsed time alone never changes an outcome.** A timeout makes an
   operation uncertain; it never changes its generation, target node or result.

## Identity and fencing

A sandbox incarnation is identified by:

| Field | Meaning |
| --- | --- |
| `deployment_id` | Prevents cross-deployment admission. |
| `sandbox_id` | Stable user-facing name. |
| `generation` | Positive, monotonically increasing integer allocated by the control plane. Never reused, including after deletion. |
| `operation_id` | Stable random identifier for one create, delete or cancel intent. Every retry of that intent reuses it. Park and wake carry their own operation IDs. |
| `spec_hash` | Canonical hash of the desired sandbox spec, excluding the operation envelope and transport metadata. |

The gateway keeps a generation high-water mark per sandbox ID
(`sandbox_generation_hwm`) that outlives the route. The node keeps a tombstone
with the greatest accepted generation after deletion. A delayed request at or
below that generation cannot recreate or delete a later incarnation.

A route names an exact deployment, sandbox ID, generation, node ID and node
epoch. Heartbeats may confirm an assigned incarnation but cannot create a new
generation or move a route to another node.

## Durable operation order

The control plane persists an intent before making a network call, and the
operation moves through these logical states: **intent** (persisted, not yet
sent), **dispatching**, **observed** (a node receipt or inventory confirms the
exact incarnation), **uncertain** (the outcome is unknown), **canceling** and
**tombstoned**. In the routing store a create intent is the route row in state
`creating` with its `create_operation_id` and `spec_hash`; a delete intent is the
route's `delete_operation_id`; the tombstone is the retained high-water mark and
the node's tombstone. An uncertain operation keeps its generation, node and
operation ID until evidence settles it.

The node serializes operations for a sandbox and applies these rules atomically
with its registry:

1. The same generation, operation ID and spec hash is an idempotent replay.
2. The same generation with a different operation ID or spec hash is a
   conflict.
3. A lower generation is stale and has no side effect.
4. A higher generation cannot replace a live lower generation. The lower
   incarnation must first be deleted or canceled and fenced.
5. Delete succeeds idempotently when the matching tombstone already exists.
6. Delete never acts on a live generation newer than the request.

Runtime objects carry generation, operation ID and spec-hash labels. On restart
the node reconciles its registry with those labels before serving mutations.
This closes the crash window between a runtime side effect and the registry
write.

Queued creates are bound to their generation in the same routing transaction
that writes the route, so replaying a command cannot recreate an explicitly
deleted incarnation.

## Inventory and heartbeats

Each node heartbeat (pushed to `POST /v1/nodes/heartbeat` every
`heartbeat_interval_seconds`) includes:

- `node_epoch`, which changes when the host boots;
- a boot-scoped `activity_epoch`, monotonic across node-agent restarts and
  advanced by durable or transient sandbox mutations;
- `inventory_complete`, distinguishing an empty inventory from an unavailable
  one;
- an inventory entry for every live sandbox with its generation, operation ID,
  spec hash, state and resources;
- separate live usage, create reservations, build reservations and physical
  disk telemetry.

The gateway stamps its own receipt time on each push. A node is fresh for
`gateway_heartbeat_ttl_seconds`.

### Park and wake acknowledgements

Synchronous park and wake acknowledgements carry the node's `node_epoch` and a
post-transition `activity_epoch`. The gateway accepts that proof only from the
exact routed node and atomically commits the new stable state with the newer
revision. Ordinary activity against a parked route first performs the same
fenced wake, so a heartbeat sampled before the transition cannot later restore
the old lifecycle state. Nodes advertise this contract as `hibernate-local-v2`;
the scheduler places parkable sandboxes only on nodes that advertise it.

### Absence is not cancellation

Inventory may confirm a matching operation, but absence alone does not cancel
an in-flight request: the request might arrive after the snapshot. The control
plane retries the same operation ID on the same node. It may place a new
generation elsewhere only after one of these fences:

- the old node durably acknowledges cancel or delete of the generation;
- the boot-epoch retirement or owner-loss classification below records the
  incarnation as lost; or
- a node-specific recovery protocol proves the operation rejected and records
  a tombstone or high-water mark.

A create that a node definitely rejected may move to another node, as a fresh
generation, so a late answer for the old one stays fenced.

### Heartbeat pull

A late push is not silence. Before the gateway answers exec, file, park, wake,
DELETE, create-replay or exec-session traffic with a retryable 503, it pulls
`GET /v1/heartbeat` from the stored node URL (2 s timeout). Concurrent requests
for one node boot share one pull; the next pull waits at least 2 s after it
ends, doubling up to 32 s while the node does not answer. A sample counts only
if its node, job, deployment, agent version and URL match the stored
heartbeat, and it is ingested like a push: its inventory reconciles routes, and
a new epoch is a reboot (class 4). A successful pull lets the request proceed,
so every request type agrees. New placement does not pull; it skips the stale
node.

## Boot epochs

A heartbeat authenticated with a new `node_epoch` proves that every process of
the earlier boot is gone, not its disk. The control store retires the old epoch
(later heartbeats from it are refused) and, in the same write, appends the
receipt time to the label `ucloud-sandboxes/controller-epoch-retirements` (the
last four are kept). The gateway emits `node_epoch_retired`.

`retire_node_epochs` then settles every old-boot route of the job in one routing
transaction, idempotently on every heartbeat:

| Old-boot route | Outcome |
| --- | --- |
| Attached, and the new boot's complete inventory reports its exact incarnation (generation, operation ID, spec hash) `parked` | Re-adopted into the new epoch; wakes there. Its exec sessions are lost. |
| Carries a recorded client DELETE | Kept as a pending delete until the node confirms it. |
| Published (portable) park | Detached; wakes on any storage-native node. |
| Anything else: running, paused, capture without COMPLETE | Deleted with a loss row, reason `rebooted`. |

At startup the node marks a RUNNING journal whose sentry is dead as
recovery-required, quarantines an ERROR volume instead of mounting it, and
reports `parked` only with a valid COMPLETE manifest: an interrupted restore
rolls back to parked; an interrupted capture without COMPLETE does not.
Ordinary inventory never adopts or retires another boot's route.

Off the heartbeat thread, the reboot reaper then sends generation-fenced node
DELETEs (one pass per job at a time, at most 32 per pass, 60 s each) for the
pending client deletes and for each new-boot inventory entry whose incarnation
has a `rebooted` loss row. A confirmed client delete removes its route. The
work is derived from durable state on each heartbeat, so a crash or failed call
waits for the next one. This frees the old registrations' reservations and IDs
so the node rejoins placement.

## Owner loss

Provider state that is merely unschedulable is not destructive proof; for
example, a powered-off Hetzner server keeps its routes and local state. When a
node owner is proven lost (a final provider state, a definitely terminated
job, or a destructive loss proof), one route classifier decides:

- published portable parks become detached and remain wakeable elsewhere;
- delete-intent routes terminate without replacement demand;
- all other routes are deleted with a loss row and answer 410 `node_lost`.

A detached park never migrates to its former owner or to a node whose inventory
still holds its ID. Creating an ID a node still registers answers 409
`sandbox_registration_conflict`. Loss rows are kept for 7 days; during that
time the lost incarnation answers 410 `node_lost` with `reason`,
`retryable: false`, `sandbox_generation` and `lost_at`.

## Failure classes

### 1. Late heartbeat, node alive

- **Detection.** The receipt is older than `gateway_heartbeat_ttl_seconds`.
- **Clients.** The heartbeat pull above. If it succeeds the request proceeds.
- **Controller.** UCloud quarantines the node
  (`heartbeat_continuity_unverified`, admission closed) and probes it directly
  in the same cycle (3 s timeout, 8 in parallel); a matching boot and route
  inventory recovers it at once. Hetzner has no quarantine: the node counts as
  unreachable and may be replaced one-for-one within `max_nodes`.
- **Node.** Nothing. An agent restart within one boot adopts live sandboxes and
  settles interrupted parks and wakes.
- **Termination.** Occupied: never. Empty: only the unreachable-empty proof,
  which a live node fails because it answers the probe (its same-boot answer is
  stored as its heartbeat).

### 2. Unreachable on push, pull and probe

- **Detection.** Class 1 plus a failed pull and a failed direct probe. Only a
  transport error fails; an HTTP, auth or schema answer proves contact.
- **Clients.** Exec, files, park and DELETE get 503
  `sandbox_worker_unreachable`, `retryable: true`, `Retry-After: 1`. DELETE
  first records a durable `delete_operation_id`; later traffic gets 409
  `sandbox_delete_pending`. Wake of a local park gets 503
  `wake_destination_unavailable`. A detached published park migrates and wakes
  elsewhere; an attached one cannot, because migration needs the source to
  answer `/migration/prepare`. Status reports `unknown`. No route is deleted and
  no loss is recorded.
- **Controller.** Each executing cycle replays recorded deletes, least recently
  attempted first (attempt times are stamped before the calls), up to
  `autoscaler_max_pending_delete_retries_per_cycle` per cycle, eight at a time,
  30 s each; the operation ID makes any replay safe. On UCloud the node is
  quarantined and `unavailable`: it keeps its billing slot, counts as
  unreachable, is skipped by that replay, cold offload and soft drain, and is
  never stop-eligible. On Hetzner it is unreachable and replaceable
  one-for-one; an empty one may meet the unreachable-empty proof.
- **Node.** If the partition heals within the boot, nothing. UCloud recovery
  needs a probe from the quarantined boot with complete inventory, no in-flight
  creates, and every assigned route present with the same generation, create
  operation and spec hash. Quarantined ingest retires no routes, so a verified
  same-boot probe first retires the routes its complete inventory omits by the
  ordinary reconcile rules (in-flight and newer-activity routes stay protected);
  one vanished sandbox does not pin the quarantine. Recovery never crosses a
  boot change.
- **Termination.** Occupied: never automatically, on either provider; an
  operator decides. Empty: the unreachable-empty proof.

### 3. Provider suspension or power-off

- **Detection.** UCloud: a current post-start `SUSPENDED` is `unavailable`. A
  RUNNING job whose history holds a timed post-start suspension carries
  `interrupted_at` and is quarantined (`provider_readiness_unverified`) only if
  that is newer than the label
  `ucloud-sandboxes/controller-continuity-verified-through`, which recovery
  writes; a historical suspension quarantines once, not every cycle. An untimed
  one stays `unavailable`. Hetzner: `off` and `stopping` are `unavailable` (the
  disk survives); `starting`, `migrating`, `rebuilding` and unknown statuses
  are `provisioning`.
- **Clients.** Creates get 503 `no_ready_node` if no other node fits. Existing
  work keeps serving while the heartbeat is fresh. While its boot is unchanged
  (`ucloud-sandboxes/controller-quarantine-epoch` equals `node_epoch`), a
  quarantined node still wakes its own local parks. A powered-off node is
  silent: class 2 answers.
- **Controller.** UCloud probes every cycle and recovers on `RUNNING` plus
  continuity, recording the interruption it covered. An off Hetzner server
  stays in `max_nodes` and the unreachable count (replaced one-for-one), shows
  as `unavailable`, and is never stopped automatically.
- **Node and termination.** A readiness blip needs nothing; a real reboot is
  class 4. Nothing terminates: an off Hetzner server stays billed until an
  operator powers it on (class 4) or deletes it (class 5).

### 4. Reboot: a new authenticated `node_epoch`

- **Detection.** The first pushed or pulled heartbeat of the new boot.
- **Gateway.** [Boot epochs](#boot-epochs): retirement, re-adoption of complete
  local parks, and the reboot reaper.
- **Clients.** Lost incarnations: 410 `node_lost`, `reason: rebooted`; exec
  sessions 410 `exec_worker_lost`; DELETE answers 200
  `{"ok": true, "deleted": false}`. Re-adopted parks wake normally. After a
  delivered client delete, GET is 404.
- **Controller.** The node rejoins the pool. On UCloud the power-off shows as a
  suspension and quarantines it; once ingest has retired the old boot, the
  quarantine is re-anchored on the new one, whose routes then prove continuity,
  and the next cycle recovers it. Two epoch retirements for one job within 24 h
  mark a failing host: it stops counting as capacity, takes the soft-drain slot
  regardless of demand (with `policy.drain_on_park_enabled`, the default), and
  stops through the ordinary drain handshake once idle.
- **Termination.** No direct path: the new guest is an ordinary node, stopped
  by the drain proof when idle; a repeatedly rebooting one is emptied first.

### 5. Provider deletion or termination

- **Detection.** UCloud final states, a re-retrieve when a job vanishes from the
  per-state query, and a full census every 300 s. Hetzner `deleting`, absence
  from the listing, or 404 on terminate. The autoscaler's own accepted stops.
- **Clients.** Until a cycle observes the final state the node is merely silent
  (class 2 answers). That cycle prunes its heartbeat and deletes the routes of
  final, definitely terminated or destructively lost jobs as 410 `node_lost`
  (`reason: node_lost`), exec sessions 410 `exec_worker_lost`. Published parks
  detach and wake elsewhere. An executing cycle also deletes, as `node_lost`,
  routes whose job is no longer an active provider job and whose node is not
  fresh, after a stale-route grace of `max(3 × TTL, TTL + 60 s)`.
- **Controller.** Retires the job's drain intents, prunes orphaned heartbeats
  and replans. **Node:** gone, with every unpublished checkpoint.
  **Termination:** already done.

### 6. Drain and scale-down

- **Detection.** A fresh idle node past `max(policy.scale_down_idle_seconds,
  2 × provisioning p95)`, an incompatible agent version, or the
  repeated-reboot rule.
- **Clients.** No new placement; existing traffic keeps working. Parks moved by
  detach (`autoscaler_max_storage_native_detaches_per_cycle` per cycle) or by
  soft drain (`policy.drain_on_park_moves_per_cycle`) wake elsewhere.
- **Controller, node, termination.** The [drain handshake](#draining). Demand
  cancels a drain; a quarantined node cannot be drained to termination. Only
  the drain proof stops it.

## Draining

Draining is a handshake, not a heartbeat boolean. The autoscaler records a
durable drain intent with a random `drain_token` and sends `POST /v1/drain` with
it. The node persists the token in its drain row, rejects new create and build
admission, publishes parks on request, and reports the token with its activity
epoch. The VM may be stopped only when a fresh, gateway-stamped heartbeat
reports the same token, complete inventory, and zero live work and
reservations. Any new activity invalidates the zero-work observation and
requires another fresh acknowledgement.

## Termination proofs

The stop journal records one of these before any provider call:

- **Drain proof** (`drainToken`, `drainReady`): a fresh, gateway-stamped,
  complete, empty inventory acknowledging the same token and activity epoch,
  with zero used and reserved resources.
- **Unreachable-empty proof** (`unreachableStaleReady`): phase `running`;
  receipt older than `policy.unreachable_stop_after_seconds`; no gateway
  routes; a last heartbeat with complete empty inventory and zero used and
  reserved resources (`lastHeartbeatSafeToStop`); and either no heartbeat ever
  (`lastHeartbeatPresent: false`) or a transport failure of this cycle's direct
  probe (`directProbeFailed: true`). A quarantined UCloud node is
  `unavailable`, not `running`, so on UCloud this proof retires only VMs that
  never heartbeated.
- **Provider destructive loss:** `destructive_instance_losses` is empty for both
  in-tree adapters, so no provider observation is destructive proof.

## Autoscaler and provider operations

Only the process holding the deployment-scoped POSIX autoscaler lock may start
provider operations. The kernel releases the lock when that process exits;
multi-host controllers are outside this protocol. Every VM create or stop has a
journaled operation ID (states `prepared`, `uncertain`, `accepted`, `settled`,
`failed`) recorded before the provider call. A provider mutation result is
`accepted`, `rejected` or `uncertain`; uncertain is never treated as rejected.

VM creates carry the operation ID in the provider label
`ucloud-sandboxes/provider-operation`. After a crash, recovery correlates that
label against a complete provider inventory; it never blindly resubmits an
unresolved create. Exactly one matching job settles the operation; several
matching jobs are reported as a conflict for explicit reconciliation; none
leaves it unresolved.

The process lock cannot cancel an already in-flight provider request, so the
operation journal and provider labels remain required even with one local
mutating controller.

## Timers

| Timer | Default | Effect |
| --- | --- | --- |
| `heartbeat_interval_seconds` | 20 s | Push cadence. |
| `gateway_heartbeat_ttl_seconds` (and `policy.heartbeat_ttl_seconds`) | 120 s | Stale: pull, then retryable 503; UCloud quarantine. |
| Heartbeat pull (constants) | 2 s timeout; 2 s apart, doubling to 32 s | Stands in for a late push. |
| `autoscaler_interval_seconds` | 5 s | Probe, recovery and replay cadence. |
| Direct probe (constants) | 3 s timeout, 8 in parallel | Quarantine recovery; unreachable-empty proof. |
| `policy.unreachable_stop_after_seconds` | 1800 s | Earliest unreachable-empty stop. |
| Stale-route grace | `max(3 × TTL, TTL + 60 s)`, 360 s by default | `node_lost` for routes of vanished jobs. |
| Repeated reboot (constants) | 2 retirements in 24 h | Soft-drain and retire. |
| Reboot reaper (constants) | 32 per pass, 60 s each | Frees old-boot registrations. |
| `autoscaler_max_pending_delete_retries_per_cycle` | 16 (8 parallel, 30 s each) | Delivers recorded deletes. |
| `policy.scale_down_idle_seconds`; `policy.builder_scale_down_idle_seconds` | 600 s; 900 s | Idle grace before drain. |
| `policy.max_stop_per_cycle` | 1 | Stop budget. |
| `autoscaler_max_storage_native_detaches_per_cycle`; `policy.drain_on_park_moves_per_cycle` | 2; 4 | Park moves. |
| UCloud full census | 300 s | Final-state backstop. |
| Loss record retention | 7 days | How long 410 answers last. |

No timer turns silence or a provider status into loss, or terminates an
occupied VM.

## Events

These events go to `metric_events` in the gateway's `metrics.sqlite`:

| Event | Emitted by | Fields |
| --- | --- | --- |
| `vm_observed` | Autoscaler, on any change of provider state, note, readiness, freshness or `interrupted_at` | `phase`, last known `node_epoch` |
| `node_epoch_retired` | Gateway | `retired_node_epoch`, `node_epoch`, `downtime_seconds`, `retiring` |
| `sandbox_reboot_reap` | Gateway reboot reaper | `status`, `confirmed`, `client_delete` |
| `node_heartbeat_pull` | Gateway | `outcome` (`refreshed`, `epoch_changed`, `unreachable`, `identity_mismatch`, `rejected` or `error`), `receipt_age_seconds` |

An interruption followed by the same epoch is a readiness blip; one followed by
`node_epoch_retired` is a reboot.

## Known gaps

- An occupied node that never returns waits for an operator.
- Nothing alerts on an `unavailable` node.
- An untimed UCloud suspension re-quarantines every cycle.
- A Hetzner `migrating` status makes a running node unschedulable.
- A reboot reap that keeps failing for longer than the 7-day loss retention is
  forgotten.
- Whether UCloud ever reports `SUSPENDED` while the guest keeps its boot is
  unknown; the same-boot recovery path guards that case without having observed
  it.
