# Scaling policy

The autoscaler buys and releases worker VMs. The gateway places each sandbox
on one of them. This document covers both: how demand turns into VMs, how
surplus VMs are emptied and stopped safely, and how a create or wake picks its
worker.

VM startup on UCloud is slow and sometimes capacity-constrained, so the
autoscaler does not behave like a fast container scheduler. It balances two
risks: too few VMs leave sandbox requests waiting behind a slow boot, and too
many VMs pile up in the provider's queue and cost money while idle.

## The controller

The autoscaler is one command:

```sh
ucloud-sandboxes autoscaler --config /etc/ucloud-sandboxes/deployment.json [--execute] [--once]
```

It is a dry run by default. `--execute` authorizes provider and node
mutations, and `--once` exits after one cycle. In production it runs as
`ucloud-sandbox-autoscaler.service` on the gateway VM, every
`autoscaler_interval_seconds` (default 5).

There is one controller per deployment. Every executing process takes a
process-lifetime POSIX lock beside `<data_root>/autoscaler-state.sqlite`; the
kernel releases it when the process exits, so there is no lease to renew. That
SQLite file holds the provider-operation journal and the durable drain
intents.

The autoscaler reads its whole policy from the `policy` object in
`deployment.json`. Changes take effect when the service restarts. To shadow a
feedback feature, set its `*_enabled` flag to `false`: its metrics keep
updating, but it neither creates nor retains nodes.

### Pools

The autoscaler manages two pools, planned independently:

- **Sandbox nodes** run sandboxes. They carry the provider label
  `ucloud-sandboxes/node=true`. Their shape comes from the `sandbox` config
  block.
- **Builder nodes** run image builds. They carry
  `ucloud-sandboxes/builder=true` and must not carry the node label. Their
  shape comes from the `builder` block. See [images](images.md) for builds.

### Providers

A small provider interface (`ComputeProvider` in
[`providers/base.py`](../ucloud_sandboxes/providers/base.py)) translates
provider APIs into list, retrieve, create, terminate and bootstrap-access
calls. Scaling policy, routing and storage stay out of it. The autoscaler
renders provider-neutral create intents (role, name, node id, labels), and the
adapter maps them to machine types, images and networks.

| Provider | Module | Notes |
| --- | --- | --- |
| UCloud (production) | [`providers/ucloud`](../ucloud_sandboxes/providers/ucloud) | VMs are UCloud jobs. Requires per-instance continuity history and guest continuity proof (below). |
| Hetzner Cloud | [`providers/hetzner`](../ucloud_sandboxes/providers/hetzner) | Still maintained. No guest-continuity requirement. |
| Others | Python entry point group `ucloud_sandboxes.compute_providers` | A factory receives its tagged `provider` config. |

`provider.kind` selects the adapter; the rest of the `provider` object is the
adapter's own schema. [Deployment](deployment.md) covers provider settings.

A provider mutation returns `accepted`, `rejected` or `uncertain`. An
uncertain result (for example a timeout after the request was sent) is never
retried blindly: the operation journal marks it `uncertain` before the call,
and a later exhaustive inventory settles it. Absence from inventory is never
proof that a job is gone.

## Capacity model

The autoscaler and the gateway count resources the same way the nodes admit
them ([node admission and memory](node-admission-and-memory.md)):

- **CPU and memory** must fit the node's physical shape per sandbox, but
  sandbox limits are not added up as if they were usage. Live samples of CPU,
  load, available memory, swap and pressure stall decide whether a node can
  take more work.
- **Disk** is a hard, additive reservation and is never overallocated. It is
  credited only from the storage-native capacity report in a fresh, complete
  heartbeat. A node without that report gets no sandboxes.

Before a node has heartbeated, the planner assumes the configured node shape.
It is derived from the `sandbox` block: `default_vcpu`, `default_memory_mb`,
and `disk_gb` minus image store (`docker_quota_image_gb`), swap (`swap_gb`),
block cache (`storage_native_cache_gb`) and headroom
(`direct_disk_headroom_mb`). With the defaults that is 32 vCPU, 96 GiB RAM and
1,449,984 MiB of sandbox disk on a 2,000 GB worker. A heartbeat replaces the
estimate with measured capacity.

## Scaling the sandbox pool up

Each cycle the planner adds up demand, subtracts what ready and booting nodes
can still take, and creates VMs for the deficit, subject to caps.

### Demand

- **Pending demand.** When a create or wake cannot be placed, the gateway
  records its exact resource shape as pending demand and answers 503; the
  caller retries. Pending demand expires 300 seconds after the last failed
  attempt, so abandoned clients do not keep VMs alive. A create already
  dispatched to a node returns a retryable "in progress" answer on retry
  instead of adding demand.
- **Prepared capacity.** `POST /v1/capacity/prepare` with `count`, `cpus`,
  `memory_mb`, `disk_mb`, optional `image`, `parkable` and `ttl_seconds`
  (default 900) announces a batch before its first create, so VMs start
  booting early. Each matching create claims one unit; unclaimed units expire
  at the TTL or are canceled with `DELETE /v1/capacity/prepare/<id>`. For
  parkable sandboxes, set `parkable: true`: the gateway then reserves the full
  hibernation disk, matching later admission. With `image`, the gateway also
  pulls the image onto enough ready nodes, or tracks a warmup until nodes
  appear.
- **Warm resources.** `policy.warm_resources` keeps a standing buffer of CPU,
  memory and disk. Keep it at zero unless a measured latency target justifies
  paying for idle capacity.
- **Build activity.** While image builds are pending or active, the sandbox
  pool adds a small probe of at most 1 vCPU, 512 MiB and 1 GiB. This keeps
  one sandbox node from scaling to zero while a build is about to produce an
  image, without reserving a whole empty node.

### Forecasting cold demand

Runs usually start cold, so pending and prepared sandboxes are sized from
earlier runs rather than from their limits:

- **Memory.** `UsageHistory`
  ([`usage_history.py`](../ucloud_sandboxes/usage_history.py)) keeps the peak
  measured memory per image and requested shape, from heartbeat samples of
  running sandboxes. It lives in `<data_root>/usage-history.json` and entries
  expire after 30 days. Cold demand is charged min(request, 1.25 × peak). The
  lookup tries the exact image and shape, then the same image at any shape,
  then the same shape across images. With no history it uses the request.
- **Disk.** Cold demand is charged what a worker actually reserves at create:
  the initial workspace grant plus the idle memory claim, from worker
  heartbeats, or from the deployment config when no worker exists yet. Growth
  is reserved later as the sandbox writes.
- The memory charge is then divided by `target_memory_utilization`, as is
  the measured working set of ready nodes plus their in-progress creates and
  wakes. Parked sandboxes contribute nothing.

A low forecast only delays scale-up: node admission still owns execution
safety, and placement checks real free disk. With this forecast, 100 small
announced sandboxes planned two workers where full requests planned three
(`tests/test_usage_history.py`).

### Booting VMs and caps

- `max_create_per_cycle` limits VMs submitted per cycle.
- `max_provisioning_nodes` caps queued or booting VMs. Keep it low while the
  provider reports scarce machines, or several VMs wait in the same queue.
- `provisioning_capacity_weight` scales how much a booting VM counts against
  demand. `1.0` is optimistic; 0.5–0.75 is safer when boot time varies.
- After `stale_provisioning_after_seconds` in a pre-start state, a VM's credit
  falls to `stale_provisioning_capacity_weight` (default 0). It still counts
  against `max_nodes` and `max_provisioning_nodes` until the provider reports
  it final, so a stuck job cannot cause duplicate submissions.
- `max_nodes` caps the pool; `min_nodes` keeps a floor.
- A running VM whose heartbeat has expired still counts toward `max_nodes`,
  but may be replaced one for one.

### Pressure feedback

Demand accounting misses some cases: a full node can still be overloaded, and
a burst can queue on startup slots. Three signals add headroom:

- **Live pressure** (`live_pressure_enabled`). The controller reads recent
  heartbeat samples from `metrics.sqlite`. A sample is pressure when a node
  with active work crosses `target_cpu_utilization`,
  `target_memory_utilization`, `max_memory_psi_full_avg10`,
  `max_io_psi_full_avg10`, or `target_storage_queue_utilization` for its
  storage or image-materialization queue. Queue pressure is
  waiting ÷ concurrency: occupied slots are useful work. At least
  `live_pressure_min_samples` samples in `live_pressure_window_seconds`, the
  latest within `live_pressure_fresh_seconds`, add a node, unless a ready
  node is idle.
- **Create pressure** (`create_pressure_enabled`). Sampled gateway create
  rejections amplify live pressure, but never act alone: a burst of healthy
  cold creates is not a reason to buy a VM.
- **Startup backlog.** If at least `create_target_concurrency_per_node`
  capacity requests are waiting, the oldest has waited
  `create_pressure_window_seconds`, and every ready node is busy with fresh
  observations, the planner may add a node even below the CPU threshold.

Both feedback paths together add at most `create_pressure_max_headroom_nodes`
(default 1) above the resource baseline, and booting nodes count toward it.
Once live pressure is sustained, pending demand can grow the pool toward
`max_nodes` at `create_target_concurrency_per_node` sandboxes per node.

The CPU and memory targets are the main density-versus-latency trade-off;
lower targets keep more headroom. The stall and queue limits catch cases that
average utilization misses.

## Scaling the sandbox pool down

### Idle grace

`scale_down_idle_seconds` counts from when a heartbeat first reports zero
active sandboxes, not from boot. Two things extend it:

- `pressure_scale_down_cooldown_seconds` after the last pressure sample.
- Measured provisioning p95 (submit to first heartbeat, over
  `provisioning_latency_lookback_seconds`) times
  `provisioning_scale_down_multiplier`. With a 70-second p95 and multiplier 2,
  an idle node is kept at least 140 seconds.

`max_stop_per_cycle` limits stops per cycle.

### Drain before stop

The autoscaler never terminates a VM straight from a scale-down decision.
Every stop goes through a drain handshake:

1. Write a durable drain intent with a random token for the deployment and
   provider job, then post `{"token":..., "draining":true}` to the node's
   `/v1/drain`. Restarts and later cycles adopt the same intent and token.
2. Each cycle re-evaluates the drain as if admission were open. If demand now
   needs the node, the intent becomes `canceling` and the controller posts
   `"draining":false`; it retires only on the node's exact acknowledgment. An
   ambiguous undrain can never authorize a stop.
3. Wait until running sandboxes are gone and every remaining route is a
   `storage-native-v1` park. Publish any park without a durable registry
   descriptor, store the descriptor in the gateway, then evict the park from
   the worker (`sandbox-detach-published-v1`). A later wake imports the
   descriptor on another node. At most
   `autoscaler_max_storage_native_detaches_per_cycle` detaches run per cycle.
4. Wait for a fresh heartbeat, stamped on gateway receipt, that proves: the
   node is draining with the matching token and closed admission; the
   inventory is complete and empty; the drain's activity epoch is current;
   active sandbox and build counts are zero; used and reserved resources are
   zero.
5. Only then journal and execute the provider stop. So two executing cycles
   are normally needed: one to drain, one to stop.

A draining node counts toward the pool but offers no placement capacity. A
failed publication leaves the park attached and blocks the stop. An
ambiguous eviction leaves the route `detaching`, which is not free until a
retry or a complete heartbeat proves the copy gone.

Publication also bounds the snapshot chain: the old remote layers plus the
new local delta are compacted when they exceed eight layers or 4 GiB of delta
beyond the base. Compaction streams one flattened layer to the registry, and
the old layers remain authoritative until the new manifest is durable.

The node agent returns `node_admission_closed` before provisioning when its
admission gate is closed. The gateway treats that as a definite rejection:
it removes the provisional route, records pending demand and lets the SDK
retry elsewhere. Timeouts and generic 5xx answers stay ambiguous, because the
create may have started.

### Drain on park

A busy RL fleet is never idle, so idle grace alone never removes surplus
workers. With `drain_on_park_enabled` (default on), the autoscaler soft-drains
one surplus worker at a time and empties it through cheap parked moves
(about 0.6–0.8 s per move):

- **Selection.** Only in a cycle that plans no creates and has no resource
  deficit, pending demand or pressure cooldown, above `min_nodes`, with at
  least two busy ready workers. A worker qualifies when demand still fits
  without it, counting the disk its parks hold. The one with the fewest
  sandboxes wins; ties go to the newest.
- **Marking.** The controller sets the heartbeat label
  `ucloud-sandboxes/soft-drain`. Admission stays open: the worker keeps
  serving execs and local wakes. Creates use it only when nothing else fits,
  migrations never target it, and scale-up does not count its capacity.
- **Moves.** Each cycle posts gateway migrations for up to
  `drain_on_park_moves_per_cycle` (default 4) of its parks, with the stable id
  `drain-<job>-<sandbox>-<generation>`. The move first asks the owner to
  publish or reuse its snapshot. Running sandboxes move after they park. A
  move without a destination is retried next cycle and records no demand; a
  move that fails before route commit rolls back so the source can wake.
- **CPU features.** Checkpoint import requires the same runtime fingerprint,
  including CPU feature flags, which can differ between VMs of the same
  model. Workers advertise `runtime-cpu-features-sha256:<hash>`, and a worker
  is only soft-drained while another ready worker shares its features.
- **Release.** The label clears when the worker is no longer surplus or could
  serve pending demand, so new demand reopens it instead of buying a VM.
- **Stop.** Once idle, a soft-drained worker uses
  min(`scale_down_idle_seconds`, 60) as its grace, then takes the normal drain
  and stop path.

In a 60-sandbox test with an exec every 5–15 s on every sandbox, a surplus
worker emptied in about 2.5 minutes and was stopped, with no failed execs
(p50 about 0.6 s).

### Retiring workers

A worker that serves what it holds but takes no new demand is *retiring*. It
takes the soft-drain slot regardless of demand and stops through the normal
handshake once empty:

- **Repeated reboots.** The gateway records each proven guest boot change in
  the label `ucloud-sandboxes/controller-epoch-retirements` (last four). Two
  within 24 hours mark a failing host. One reboot loses the old guest's
  processes, not the node: running routes from that boot are retired, and
  published snapshots remain recoverable.
- **Slow host.** With `max_cpu_check_ms` above 0, a worker whose boot CPU check
  took longer than that limit retires. Off by default.

### Unavailable and unreachable workers

Silence is never proof of loss.

- **UCloud continuity.** A UCloud job that is suspended after start, or has a
  RUNNING → SUSPENDED → RUNNING history, is *unavailable*, not safe to
  destroy. The controller quarantines it from new placement while keeping its
  routes and inventory. Quarantine clears only on provider RUNNING plus a
  fresh, authenticated direct heartbeat with the same guest boot and a
  complete inventory matching the assigned routes. The label
  `ucloud-sandboxes/controller-continuity-verified-through` records which
  suspension was covered, so a historical suspension quarantines once. A
  quarantined worker still wakes its own parks.
- **Unreachable empty workers.** After `unreachable_stop_after_seconds`, a VM
  may be stopped without a drain acknowledgment only if it has no routes and
  either never heartbeated, or its last complete inventory was empty and a
  direct probe in the same cycle failed in transport. UCloud never deletes an
  occupied VM on silence.
- Quarantined and powered-off VMs keep their provider slots and count as
  unreachable; a persistently unavailable VM needs an operator.

For incident reproduction, stop the executing autoscaler.
`max_stop_per_cycle: 0` is not a global kill switch for provider mutations.

### Cleanup work

Each executing cycle also replays recorded client deletes, least recently
attempted first, at most `autoscaler_max_pending_delete_retries_per_cycle`
(eight in parallel, 30-second timeout each). A delete that meets an
uncommitted migration aborts the destination import first. Active migration
journals whose route is already gone are closed in the database, at most
`autoscaler_max_orphaned_migration_reconciles_per_cycle` per cycle. Results
appear as `pending_delete_attempts`, `orphaned_migration_reconciles` and
`storage_detach_attempts` in the autoscaler execution metrics.

### Consolidation on wake

`parked_wake_consolidation_enabled` (default off) lets a published park wake
on a busier worker even when its own worker could run it, so sparse workers
empty over successive park/wake cycles. It requires:

- source CPU at or below half `target_cpu_utilization`, and a destination
  with at least as many active sandboxes and a strictly lower job rank, so
  sandboxes cannot bounce back;
- fresh complete inventories and samples, open admission, and no concurrent
  creates or storage errors on either worker;
- a cached exact image on the destination, migration capability, disk
  admission and room for the full waking shape under the utilization and
  pressure targets (a larger fresh `memory_observation` replaces the declared
  memory);
- no active migration anywhere, and 60 seconds since the last consolidation
  in this gateway process.

The event `sandbox_wake_consolidation` records each move. Without a suitable
target the wake stays local. It never moves running sandboxes.

## Scaling the builder pool

Builder demand is a count, not resources:

- A pending image build records a one-shot scale-up signal; build callers
  retry `POST /v1/images/build` once a builder is ready.
- `POST /v1/builders/prepare` asks for `count` builders ahead of a known build
  burst. It is consumed after an executing cycle and can be canceled with
  `DELETE /v1/builders/prepare/<id>` before then.

The pool targets max(1 if builds are pending, prepared builder count), capped
by `builder.max_nodes` (default 1). Idle builders stop after
`builder.scale_down_idle_seconds` (default 900) once no builds are pending and
prepared signals are consumed. Keep that grace longer than the sandbox grace:
builds arrive in bursts and builder startup is expensive.

## Create placement

`gateway_create_placement` selects how a create picks its worker. The code
default is `ranked`; `power_of_k` is the alternative and is required for
group creates.

### Power of k

Implemented in [`placement_choice.py`](../ucloud_sandboxes/placement_choice.py)
and [`gateway/create.py`](../ucloud_sandboxes/gateway/create.py). The node's
admission is the authority; the gateway only picks candidates.

1. **Fleet view.** Candidates are fresh, admitting sandbox workers from the
   shared heartbeats, filtered by required capabilities and by an exact
   disk-and-memory fit that also counts in-flight migrations. There is no
   fleet-wide scan or lock.
2. **Sample and score.** Each API process samples k = 3 eligible workers
   uniformly and orders them by score:

   ```text
   score = live pressure + in-flight creates / create_target_concurrency_per_node
           - 0.25 × image residency
   ```

   Residency is the fraction of the image's startup bytes already on the
   node; an image this process is already sending there counts as half
   resident. In-flight creates are the node's reported creates plus this
   process's unconfirmed creates times the number of API processes, which
   estimates the other processes' traffic since the last heartbeat.
   Soft-drained workers fill the list only when fewer than k others are
   eligible.
3. **In-process claims.** Each process keeps an in-memory overlay that
   charges its own creates to their workers until a heartbeat reports the
   exact sandbox, the worker reboots, or 60 seconds pass. A planning lock
   makes concurrent creates in the same process see each other's choices.
   Nothing is shared between processes: random sampling spreads processes
   that look at the same stale view, and node rejection catches overbooking.
4. **Dispatch.** The create tries the candidates in order, for up to two
   rounds; the second round resamples without the workers already tried.
   Every candidate except the last gets `X-UCloud-Admission-Wait: 1`, so a
   busy node rejects after one second instead of holding the create for its
   full admission wait (30 seconds); the last gets no header and the full
   wait, so a busy fleet does not reject all k at once. The gateway strips
   any client copy of the header, and nodes ignore a malformed value.
5. **Outcomes.** Acceptance confirms the route. A definite rejection
   (including at the image check) releases the overlay charge and retargets
   the route to the next candidate as a fresh generation and operation id, so
   a late answer from the rejecting node stays fenced. An ambiguous answer
   keeps the route and the charge. If every candidate rejects, the route is
   deleted, fenced pending demand is recorded and the client gets a 503 naming
   the last rejection.

In this mode the API processes run creates and groups themselves; the
placement process only runs wakes.

### Ranked

`ranked` scans the fleet and reserves capacity transactionally under a
placement lock. It ranks each candidate by a load of live pressure plus
in-flight creates ÷ `create_target_concurrency_per_node`:

1. Soft-drained workers last.
2. Workers below a load of 0.6 before busy ones. Busy workers keep the older
   order, assigned shapes then load, so a stale heartbeat cannot funnel a
   burst onto one peer.
3. Below that band: workers holding the image, then with the image in
   flight, then neither; then fewest missing layer bytes.
4. Assigned shapes, load, in-flight creates, slack and node id as tie
   breakers.

A same-image burst therefore stays on the worker holding the image until
about five creates are in flight (at the default target of 8), then
overflows.

### Group create

`POST /v1/sandboxes:batch` (power-of-k only) creates up to 512 sandboxes of
one spec. The default `pack` policy fills the best sampled worker before the
next, so each worker attaches the image once; `spread` caps each worker at an
even share. Rejected members are re-planned for up to four rounds, and the
rest become pending demand. See [API reference](api-reference.md).

### Concurrency limits

- New workers get `create_target_concurrency_per_node` as their startup
  concurrency. Creates, restores and buffered reads use separate FIFO queues,
  so a create burst cannot consume restore capacity. Waits are
  deadline-bound; capacity rejections carry a retryable error code and
  `Retry-After`, which the SDK retries with jitter.
- `gateway_max_concurrent_sandbox_creates` optionally caps creates across the
  fleet in FIFO order. `0` (default) disables it. Wakes, streamed reads and
  small control operations bypass it.

### Wake placement

A parked sandbox wakes on its own worker when its shape fits and live pressure
allows. Otherwise the gateway may migrate its verified storage-native snapshot
to a fitting worker first. Workers apply the same pressure brake and reserve
concurrent creates and wakes against physical CPU and RAM, so stale
heartbeats cannot cause an unbounded activation burst. A wake that cannot be
placed records pending demand like a create.

## Measurements

The controller records VM lifecycle events in `metrics.sqlite`. `GET
/v1/metrics` reports them under `vm_lifecycle`, including
`submit_to_running_ms`, `ucloud_created_to_running_ms`,
`running_to_first_init_attempt_ms`, package-stage and remote-init durations,
`first_init_attempt_to_first_heartbeat_ms` and
`first_heartbeat_to_first_sandbox_ms`. The running-to-init interval includes
the controller's polling delay. Use `?full=true` for a larger event window and
fresh registry data. Tune the caps and graces from these numbers.

## Configuration reference

### `policy`

Every key below must be present in `policy`, except the five marked
*optional*, which take their default when omitted. Unknown keys are rejected.
`heartbeat_ttl_seconds`, `builder_scale_down_idle_seconds` and
`default_node_resources` are not policy keys: they come from
`gateway_heartbeat_ttl_seconds`, `builder.scale_down_idle_seconds` and the
`sandbox` block.

| Key | Default | Meaning |
| --- | --- | --- |
| `min_nodes` | 0 | Sandbox pool floor. |
| `max_nodes` | 10 | Sandbox pool cap, including booting and unreachable VMs. |
| `warm_resources` | 0/0/0 | Standing `vcpu`, `memory_mb`, `disk_mb` buffer. |
| `max_create_per_cycle` | 4 | VMs submitted per cycle. |
| `max_stop_per_cycle` | 1 | VMs stopped per cycle. |
| `max_provisioning_nodes` | 8 | Cap on queued or booting VMs. |
| `provisioning_capacity_weight` | 1.0 | Credit for a booting VM (0–1). |
| `stale_provisioning_after_seconds` | 300 | Pre-start age after which a VM is stale. |
| `stale_provisioning_capacity_weight` | 0.0 | Credit for a stale VM (0–1). |
| `max_cpu_check_ms` | 0 | *Optional.* Retire workers whose boot CPU check exceeds this; 0 is off. |
| `unreachable_stop_after_seconds` | 1800 | Silence before an empty unreachable VM may be stopped. |
| `scale_down_idle_seconds` | 600 | Idle grace before a sandbox node drains. |
| `live_pressure_enabled` | true | Use live node pressure. |
| `live_pressure_window_seconds` | 60 | Pressure sample window. |
| `live_pressure_min_samples` | 3 | Samples in the window to act. |
| `live_pressure_fresh_seconds` | 30 | Maximum age of the latest sample. |
| `target_cpu_utilization` | 0.70 | CPU pressure threshold. |
| `target_memory_utilization` | 0.80 | Memory pressure threshold and forecast divisor. |
| `max_memory_psi_full_avg10` | 5.0 | Memory full-stall threshold. |
| `max_io_psi_full_avg10` | 10.0 | *Optional.* I/O full-stall threshold (0–100). |
| `target_storage_queue_utilization` | 0.75 | Storage and image-materialization queue threshold. |
| `create_pressure_enabled` | true | Use gateway create pressure and startup backlog. |
| `create_pressure_window_seconds` | 30 | Create pressure window; startup backlog age. |
| `create_pressure_min_samples` | 2 | Samples to act. |
| `create_pressure_fresh_seconds` | 15 | Maximum sample age. |
| `create_target_concurrency_per_node` | 8 | Per-node create target: worker startup slots, placement load, backlog threshold. |
| `create_pressure_max_headroom_nodes` | 1 | Nodes feedback may add above the baseline. |
| `pressure_scale_down_cooldown_seconds` | 300 | Hold capacity after the last pressure sample. |
| `provisioning_latency_lookback_seconds` | 604800 | Window for provisioning p95. |
| `provisioning_scale_down_multiplier` | 2.0 | Idle grace floor = provisioning p95 × this. |
| `parked_wake_consolidation_enabled` | false | *Optional.* Consolidation on wake. |
| `drain_on_park_enabled` | true | *Optional.* Soft-drain surplus workers through parked moves. |
| `drain_on_park_moves_per_cycle` | 4 | *Optional.* Parked moves per cycle. |

### Other keys

| Key | Default | Meaning |
| --- | --- | --- |
| `autoscaler_interval_seconds` | 5.0 | Cycle interval. |
| `autoscaler_max_init_per_cycle` | 4 | VM initializations started per cycle. |
| `autoscaler_init_retry_seconds` | 30 | Delay between init attempts. |
| `autoscaler_init_timeout_seconds` | 1800 | Init deadline per VM. |
| `autoscaler_max_pending_delete_retries_per_cycle` | 16 | Client delete replays per cycle. |
| `autoscaler_max_orphaned_migration_reconciles_per_cycle` | 128 | Orphaned migration journals closed per cycle. |
| `autoscaler_max_storage_native_detaches_per_cycle` | 2 | Park detaches per cycle during drain. |
| `gateway_create_placement` | `ranked` | `ranked` or `power_of_k`. |
| `gateway_max_concurrent_sandbox_creates` | 0 | Fleet-wide create cap; 0 is off. |
| `gateway_heartbeat_ttl_seconds` | 120 | Heartbeat freshness for placement and scaling. |
| `sandbox.product_id` | `cpu-amd-zen5-32-vcpu` | Sandbox VM machine type. |
| `sandbox.default_vcpu`, `sandbox.default_memory_mb`, `sandbox.disk_gb` | 32, 98304, 2000 | Planned node shape (disk before reservations). |
| `builder.product_id` | `cpu-amd-zen5-16-vcpu` | Builder VM machine type. |
| `builder.max_nodes` | 1 | Builder pool cap. |
| `builder.scale_down_idle_seconds` | 900 | Builder idle grace. |
