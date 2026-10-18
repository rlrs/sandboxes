# Node admission and memory

This document describes how a worker node decides whether work fits, and how it
manages memory and disk for sandboxes that are running, paused or parked. The
gateway chooses a node (see [scaling-policy.md](scaling-policy.md)); everything
here happens after that choice, on the node.

Two processes share the node side. The Python node agent
(`ucloud_sandboxes/node_agent.py`, `node_runtime.py`, `direct_service.py`,
`direct_warden.py`) owns lifecycle: create, park, wake, delete and migration.
The Rust daemon `ucloud-noded` (`runtime/noded`) can take over the node port,
creates, execs, the pause tier and admission, each behind its own flag. The last
section lists which parts run where.

## Resource model

A sandbox's `vcpu`, `memory_mb` and `disk_mb` are **maximums, not
reservations**. The node treats each resource differently:

| Resource | Bound inside the sandbox | What the node charges |
|---|---|---|
| CPU | the cgroup's CPU quota | Nothing. Sandboxes share CPU through their cgroups. CPU is not part of node admission: a sampled spike must not send a queued transition to another worker. |
| Memory | the cgroup's memory limit | Live evidence plus forecasts of in-flight transitions (the transition ledger). Resident sandboxes do not reserve memory. |
| Disk | the workspace filesystem size and XFS project quotas | Hard claims in the node registry, for bytes that can physically be written. Never overcommitted. |

A request must still fit the node's physical shape: its `vcpu` and `memory_mb`
cannot exceed the node's totals.

The rest of this document follows from that split. Memory admission asks "is
there headroom now, after the work already admitted?" Disk admission asks "can
the node promise these bytes?" They use different evidence and fail
differently: a memory shortfall waits within a deadline, while a disk shortfall
is refused at once with a retryable error.

## Evidence

### Live node metrics

Admission samples the host when it decides, outside its lock, through a
single-flight cache of 200 ms:

- `MemTotal`, `MemAvailable`, swap total and free;
- memory PSI `full avg10`;
- the RAM-backing filesystem's total and available bytes, when RAM backing is
  configured (see [Memory backing](#memory-backing)).

Missing evidence never reads as plenty. A configured RAM-backing mount that is
absent or unreadable counts as zero available bytes, which queues work until
the deadline; it is never treated as unlimited.

### Per-sandbox resident samples

A resident sampler reads each candidate sandbox's cgroup v2 files once a second
(`ucloud_sandboxes/resident_memory.py`; `runtime/noded/src/pause_policy/resident.rs`).
It samples owned, parkable managed-process sandboxes and every paused sandbox.
A sample (`ResidentMemorySample`) records:

- `current_bytes`, anonymous, file, dirty and writeback bytes, file refaults;
- `shared_memory_bytes` (shmem, which is what RAM-backed guest memory is charged as);
- `peak_bytes` from `memory.peak`: the runtime's high-water mark;
- `swap_bytes` from `memory.swap.current`: pages a paused runtime moved out.

Samples are fenced by incarnation (sentry PID and start time, cgroup device and
inode). A park, restore or delete forgets them, so a sample always describes the
current runtime. Readers apply their own age limits: growth credit uses a sample
up to 30 s old, live resident decisions up to 2.5 s. A stale or missing sample
gives no credit.

### Diagnostic evidence in heartbeats

Heartbeats carry two optional, diagnostic-only objects in `runtime_metrics`.
Neither changes an admission decision.

- **`resource_evidence`** (`ucloud_sandboxes/resource_evidence.py`). One
  background thread collects it every second; readers copy a cache and see
  `null` when it is older than 5 s. It reports host dirty, writeback, mapped,
  shared and cached bytes; major faults and refaults; the agent's own cgroup
  CPU and memory counters (which may exclude sandbox processes, so never
  present them as sandbox or fleet consumption); and per-device rates for
  guest-visible leaf whole block devices. Partitions, loop, ublk and
  device-mapper devices are excluded, and the collector infers no
  filesystem-to-device or per-sandbox attribution. A zero means a counter was
  observed; `null` means it was not. It also carries cumulative
  `host_cpu_usage_usec` and `host_cpu_steal_usec`.
- **`resident_wait`**: resident wait count, checkpoint count and in-flight
  count, projected reclaim bytes, target bytes and the current policy reason.

Upgrade gateway readers before workers whenever a heartbeat gains a field:
older strict decoders reject unknown fields, while new readers accept old
heartbeats.

## Admission decisions

### Slots

Three weighted FIFO pools (`FairCapacity`, `ucloud_sandboxes/admission.py`)
bound concurrent transitions:

| Pool | Size |
|---|---|
| startup (creates) | `policy.create_target_concurrency_per_node` (default 8) |
| restore (wakes) | `sandbox.direct_max_concurrent_restores` (default 8) |
| publication | 16 lanes |

The pools do not barge: a waiter behind the head cannot overtake it, except
that a weighted head that times out lets a fitting follower through. A grant
that races a timeout is never lost, and cancelling an owner never revokes a
granted slot.

### The decision

`ucloud_sandboxes/admission_decision.py` holds the decision as pure functions;
`runtime/noded/src/admission/decide.rs` is the Rust port, checked against
goldens recorded from the Python code. One pass is:

1. **Gate** (before sampling): admission must be open (not draining), and the
   request must fit the node shape.
2. **Decide** (after sampling):
   - memory PSI `full avg10` ≥ 10 waits;
   - `MemAvailable` below the **2 GiB physical floor** waits;
   - `MemAvailable` plus free swap (or `MemAvailable` alone on a node without
     swap) below `max(2 GiB, requested memory)` waits. Free swap never
     satisfies the floor itself;
   - the ledger's projected physical demand must fit the headroom above the
     floor, and its projected RAM-backing demand must fit the RAM-backing
     filesystem's available bytes.
3. A `Wait` releases the lock, waits for a ledger change or 0.21 s (the next
   sample after the cache expires), and repeats until the deadline:
   `admission_wait_seconds` (30 s) or the caller's own budget. No lifecycle
   lock is held while waiting, so delete and drain can interrupt.

When the deadline passes, the node refuses with a retryable error. Only
refusals that happen before dispatch are presented as safe retries; a failure
after the node has started the runtime stays ambiguous.

| Error code | HTTP | Meaning |
|---|---|---|
| `node_admission_closed` | 503 | The node is draining. |
| `node_active_admission_deferred` | 503, `Retry-After: 1` | A create did not fit (slot or memory) within its deadline. |
| `node_startup_busy` | 503, `Retry-After: 1` | A managed launch or continuation did not get its growth forecast admitted. |
| `node_restore_busy` | 503, with `lifecycle_state: "parked"` | A wake did not get a restore slot or memory. The sandbox is still parked. |
| `primary_already_owned` | 409 | A different managed primary already owns this generation. |
| `park_deferred` | 409, `Retry-After` | A park could not reserve checkpoint space, or a resident wait is being kept. The sandbox keeps running. |

### The transition ledger

`TransitionLedger` (`ucloud_sandboxes/transition_admission.py`;
`runtime/noded/src/admission/ledger.rs`) is the accounting behind step 2. It is
not a scheduler; the slots, lifecycle fences and live checks remain the
authority. Each operation holds a claim with its own token, so releasing one
claim can never release a peer's.

| Kind | Memory forecast |
|---|---|
| Startup, ordinary sandbox | The requested memory bound, until the sandbox settles. |
| Startup, managed-process sandbox | 0. Its workload has not launched; the launch carries the forecast (below). |
| Restore | The authenticated checkpoint metadata's allocated bytes. A managed primary reserves `max(allocated, memory bound)`, covering growth after the copy. Missing metadata falls back to the bound. |
| Capture | Unknown. Capture relieves pressure, so it is observed but never charged and never queued. |
| Publication | Unknown. It takes a publication lane instead. |

Rules the ledger enforces:

- **Per owner, the charge is the maximum** of its forecast and its claims, not
  the sum. Repeated requests for the same incarnation share one forecast.
- **Concurrent admissions cannot spend the same headroom.** A grant charges its
  forecast before the lock is released.
- **Running work comes first.** A startup may only spend what every queued
  restore leaves.
- **Reclaim demand** (what the pause tier and resident-wait policy try to free)
  is the admitted work plus only the *next* eligible FIFO owner, not the whole
  queued backlog. A closed node bills nothing.
- **Publication** keeps one lane running during a foreground burst, but a new
  publication does not start while a foreground transition waits.
- **Delete** cancels the owner's queued waiters; **drain** cancels every queued
  startup and restore. Failed, cancelled and timed-out operations release both
  their slot and their exact claim.
- **Restore revalidates** the generation and checkpoint generation after the
  queue. Two queued wakes restore once; a changed checkpoint is requoted.

These forecasts do not measure the runtime's own overhead; the live floor and
PSI checks cover that.

### Managed growth forecasts

A finished create does not mean the workload has allocated its memory. Agents in
managed-process sandboxes typically start, allocate a large heap, then spend
most of their time waiting for a model. If admission released the create's
reservation as soon as the empty runtime started, a burst of launches could all
allocate at once with nothing accounting for it. The node therefore keeps a
**growth forecast** per managed primary.

The supervisor accepts exactly one primary job per sandbox generation. The node
records the same identity: the job ID and `launch_sha256`, the SHA-256 of the
supervisor `start` request's canonical bytes. The forecast moves through these
phases:

| Event | Forecast |
|---|---|
| Launch requested | The launch intent is committed as `queued`, then admitted to `active` **before** the supervisor RPC is sent. It carries the whole memory bound until its first safe wait. An ambiguous RPC keeps it across agent restarts. |
| Model wait (relay park or node-local wait) | Suspended. The guest's actual allocation is already visible in host memory and backing evidence. Advisory phase hints do not suspend it. |
| Continuation (the model answered) | Reacquired **before** the node acknowledges the wake: the runtime's `memory.peak` (capped at the bound) minus its current footprint, for both physical memory and RAM backing. On RAM backing only shmem counts as credit. |
| Checkpoint committed | Suspended. |
| Restore of a managed primary | Reserves `max(checkpoint allocation, bound)`, then restores the active forecast. |
| Terminal job observed, or sandbox deleted | Retired. Only a matching job and generation can retire it. |

The forecast is a forecast, not a guarantee. Charging every continuation its
whole bound held relay responses for 20–30 s at about 180 sandboxes per node
while most memory was free, so continuations are charged back to their
demonstrated peak instead. The accepted risk: a guest that grows past its
previous peak while RAM backing is nearly full can take a `SIGBUS` before
pressure relief frees space. A runtime with no fresh sample is charged its whole
bound.

Ordering details that matter:

- A continuation's grant and that request's durable wake fence
  (`relay_wake_fences`) commit in one transaction, so an older park cannot erase
  the grant. While the continuation queues, its wait stays eligible for
  ordinary pressure parking, so a burst of answers cannot remove every reclaim
  candidate at once.
- Durable growth writes never hold the node-wide admission lock. Per-key turns
  order durable changes before in-memory ones, and admitted bytes stay charged
  while a commit is in flight.
- Only the pre-dispatch growth deadline (and the analogous
  pre-acknowledgement continuation deadline) becomes `node_startup_busy`.
  Supervisor and restore failures stay ambiguous.
- An imported primary's identity is unknown until the supervisor's answer binds
  it; its first wake still goes through restore admission with the full bound.

Growth intents live in the node registry's `managed_growth` table (registry
schema v9, `ucloud_sandboxes/direct_registry.py`; `runtime/noded/src/registry/`
writes the same rows byte for byte).

## Tiers: resident, paused, parked

A sandbox that is not doing work can sit in one of these tiers. Each step down
frees more host memory and costs more to come back from.

| Tier | Where its memory is | Lifecycle state seen by the gateway | Cost to resume |
|---|---|---|---|
| Resident | In RAM (RAM backing or page cache) | running | none |
| Paused | In RAM, partly swapped out under pressure | running | a thaw: milliseconds, plus swap-in prefetch if pages were reclaimed |
| Parked | A checkpoint on the node's local disk | parked | a restore, through restore admission |
| Parked and published | The checkpoint and workspace layers also in object storage | parked | as parked |
| Offloaded | Only in object storage; the local copy was evicted | parked | a remote restore, possibly on another node |

Pausing changes no ownership: the Warden journal stays `RUNNING`, the route
stays running, and the activity revision does not move. Parking is a durable
lifecycle transition (see [state-protocol.md](state-protocol.md)).

### When each transition happens

| Trigger | Node without the pause tier | Node with `sandbox.direct_pause_tier` |
|---|---|---|
| Idle for `sandbox.direct_idle_park_seconds` (0, the default, disables it). Applies only to non-managed sandboxes: managed agents choose their own park points through model waits. | Background park (hibernate, then publish). | Pause. |
| Relay model wait (the relay asks the node to park) | Kept **resident**; checkpointed only when resources need it (resident-wait policy below). | Pause, unless the predicted wait makes a hibernate cheaper (the Aries rule below). |
| Node-local model wait (`sandbox.direct_local_model_waits`) | Not available. | Pause, decided by the node from TCP headers alone. |
| Explicit API park, drain move, offload move | Hibernate. | Hibernate. |
| Memory pressure on paused sandboxes | — | Reclaim paused memory to swap; escalate to hibernate when reclaim stalls or swap is full. |
| Disk pressure | Publish parked sandboxes, then cold-offload published ones. | Same. |

### Resident waits (no pause tier)

`WarmParkPolicy` (`ucloud_sandboxes/warm_park.py`) keeps model waits resident
while real memory headroom exists. A relay park answers `409 park_deferred`;
the node keeps the wait in a local table it re-checks every 250 ms, and the
relay's durable retry (30 s) is only a recovery backstop.

The decision (`decide_resident_wait`) is pure and uses measured pressure:

- **Reserve** = `max(5% of RAM, min(2 GiB, 10% of RAM))`. Reclaim starts when
  `MemAvailable` minus incoming demand falls to the reserve, and stops only
  above 1.5 times the reserve.
- **Incoming demand** is the ledger's reclaim demand: active transitions plus
  the next FIFO start or restore, not the configured limits of a queued burst.
- **RAM backing** is checked the same way. Unknown backing evidence triggers
  reclaim.
- **PSI alone** triggers reclaim only when spare memory is already within
  1.5 reserves and I/O PSI is below 20; checkpoint I/O can cause reclaim PSI,
  and more checkpoints would feed that loop.

When reclaim is needed, it checkpoints the waits that will stay idle longest,
with concurrency following the byte deficit. In-flight checkpoints get
provisional credit from their measured footprint, and actual `MemAvailable` is
checked again after each completes. On saturated storage the release target is
cut to 5% of RAM per cycle to avoid a checkpoint storm. Before a checkpoint, a
wait may get one measured probe that reclaims clean page cache instead; this
cannot help a RAM-backing (tmpfs) shortfall, so it is skipped then. There is no
model-wait timeout and no fixed sandbox cap.

### The pause tier

`sandbox.direct_pause_tier` (default off; needs `sandbox.swap_gb` ≥ 1) pauses
waiting sandboxes in place (`ucloud_sandboxes/pause_tier.py`;
`runtime/noded/src/pause/`, `runtime/noded/src/pause_policy/`).

**Pause.** The Warden writes a durable pause marker, then runs `runsc pause`,
which stops the guest's tasks inside the Sentry. The cgroup is not frozen: a
frozen cgroup changed no reclaim measurably and would need thawing before every
`runsc` call. The marker is the truth; scheduler state is disposable and
rebuilt from markers. Any lifecycle activity (an exec, a file read, a park)
skips a pause rather than waiting for it.

**Hibernate or pause.** For a model wait with a fresh relay phase hint
(`expected_remaining_wait_seconds`), the node hibernates instead of pausing
when `expected wait × resident bytes > 300 s × footprint bytes` (resident plus
swapped). 300 s is the break-even for keeping a byte in DRAM against writing it
to NVMe and reading it back. A park that cannot reserve checkpoint space falls
back to a pause.

**Reclaim.** Every 250 ms the policy re-derives the paused set from markers,
decides from measured pressure (the same `decide_resident_wait`), and plans
relief (`relief_plan`):

- Hinted waits are ranked by expected remaining idle time × resident bytes;
  unhinted waits by resident bytes, most recently paused first (a long-paused
  wait is the likeliest to wake next).
- A selected wait is reclaimed to swap through the cgroup's `memory.reclaim`,
  in 128 MiB windows, within one node-wide budget: at most 2 reclaims in
  flight sharing 512 MiB/s. Reclaim stops short of the last 10% of swap.
- A reclaim that frees less than 16 MiB is a stall. A stalled wait backs off
  (10 s, doubling); after 2 stalls in a row, or when swap has no room, it
  **escalates** to a durable hibernate if it still holds at least 16 MiB
  resident and more resident than swapped. At most 2 escalations run at once.
- `sandbox.direct_pause_tier_zswap` (default off) enables zswap ahead of swap
  and caps each paused cgroup's `memory.zswap.max` at 25% of its memory bound:
  incompressible pages stay nearly 1:1 in zswap's pool, so uncapped reclaim
  through zswap freed almost nothing.

**Thaw.** An exec, file operation, wake or node-local answer thaws a paused
sandbox. A thaw holds the marker's flock ("thaw in progress"), prefetches
swapped memory (8 threads, 4 MiB pieces, at most 1 GiB or 2 s; skipped below
32 MiB swapped), runs `runsc resume`, then removes the marker. Demand faults
bring back the rest.

On RAM-backed pause-tier workers the RAM-backing tmpfs is mounted swappable and
sized to include `SwapTotal`, because a swapped-out page still owns its tmpfs
block until the guest frees it.

### Park and wake

A park is a two-phase hibernation capture (the pinned gVisor patches in
[runtime/gvisor/README.md](../runtime/gvisor/README.md)):

1. Reserve checkpoint disk space (see [Memory checkpoint
   claims](#memory-checkpoint-claims)). If the node cannot, the park is
   refused with `park_deferred` and the sandbox keeps running.
2. Capture: application memory is exported sparsely, the Sentry's private
   pages and the gVisor filestore (the guest's rootfs writes) are serialized,
   and the workspace upper is sealed into an immutable local layer.
3. Make the checkpoint durable with a single COMPLETE pointer, then reap the
   runtime. A failure before COMPLETE resumes the original process; the
   original checkpoint stays authoritative until the restored runtime is
   durably `RUNNING`.

A background park (`background: true`, used by idle parks) then publishes the
checkpoint and sealed layers to object storage.

A wake runs restore admission (slot plus the restore forecast), restores into a
paused candidate, records it as the owner, then resumes the guest. The restore
populates RAM-backed memory inside the Sentry's own cgroup, so the memory is
charged to the right sandbox.

### Publication and offload

Parked sandboxes free local disk in two steps. Publication uploads the
checkpoint and workspace layers; afterwards the workspace's local claim drops to
zero once its local copy is released. Cold offload is the gateway's
(`ucloud_sandboxes/cold_offload.py`): when a node's `storage_hard_reserved_mb`
reaches 90% of capacity it detaches older, larger, already-published parks
until reservations fall to 80%. Active model waits and pending wakes are never
offloaded, and missing measurements never authorize an offload.

## Memory backing

Where a running sandbox's application memory lives is set by three flags. All
default off. Split backing is for sandbox workers only; node init refuses it on
other roles.

| Mode | Flags | Running memory | Parked memory |
|---|---|---|---|
| Combined (legacy) | none | gVisor memory file inside the sandbox's storage volume | checkpoint in the same volume |
| Split, file-backed | `direct_split_memory_backing` | a regular file in a per-sandbox XFS project-quota directory | checkpoint component in the same directory |
| Split, RAM-backed | `+ direct_ram_memory_backing` | a bounded tmpfs (`/run/ucloud-sandboxes/application-memory`) | checkpoint component on disk |
| Split, RAM-backed with reflink restore | `+ direct_reflink_memory_restore` | RAM until the first parked restore, then a private XFS reflink of the checkpoint (file-backed from then on) | checkpoint component on disk |

**Split memory backing** separates the workspace block volume from the memory
backing. The worker has one XFS filesystem with project quotas at the
storage-native mount root; workspace mounts and memory directories are distinct
child paths, and memory adds no block device per sandbox. The node bootstrap
(`ucloud_sandboxes/memory_filesystem.py`) uses a prepared project-quota
filesystem or creates one owned sparse image, records a durable receipt that
binds the image's inode and UUID, and reattaches it on later boots with direct
loop I/O. It never reformats an existing image and refuses a mount root that
already holds workspace data. The whole sandbox disk claim is admitted
atomically in the registry before either component allocates; component quotas
are secondary bounds, not separate pools. Enabling split backing on a worker
that already has sandboxes is not supported: qualify new workers with the flag
and drain old ones. Disabling it stops new split writers, but readers must stay
until split owners are gone. It requires registry checkpoint publication
(`snapshot_store.kind` = `registry`).

**RAM backing** keeps live application memory on a tmpfs sized
`95% × min(node memory, MemTotal)`. It is `noswap` unless the pause tier is on
(see above). Guest memory there is charged to the sandbox's cgroup as shmem, and
admission checks the tmpfs's available bytes separately from physical memory.
Free swap does not help a `noswap` tmpfs allocation, and clean page cache
reclaim cannot free tmpfs space; only a checkpoint can.

**Reflink restore** restores file-backed memory from a private XFS clone of the
checkpoint instead of copying it. The Warden selects file backing persistently
at a fenced parked restore, holds the source in its own exact-quota retention
project until the restored runtime is durably `RUNNING`, and has no eager-copy
fallback. The trade-off:

- It restores a 2 GiB heap in about 0.1 s instead of about 1 s, writes about a
  quarter as much per park and wake, and lets the node write back and reclaim a
  file-backed runtime's pages without parking it.
- Its memory claim is the fixed formula for the sandbox's lifetime, because the
  live memory file grows on disk (see the next section). This costs density.

## Disk claims and workspace growth

`ucloud_sandboxes/disk_claims.py` implements claims that follow demonstrated
usage. The node never promises more physical bytes than it has; there is no
overcommit ratio. When a claim cannot grow, a create, park, wake or workspace
growth is refused with a retryable error, and the shared filesystem never fills.

### What is charged

Each registration's row in `registration_disk` carries:

| Column | Meaning |
|---|---|
| `reserved_mb` | A fixed claim: legacy and imported registrations until their first claim update. |
| `workspace_mb` | The workspace grant plus its local sealed layers. |
| `memory_mb` | The memory checkpoint claim. |

The registry is the single admission authority for disk. Increases that create
physical capacity (a create's plan, grant growth, park admission, workspace
remount) are checked atomically against the node's hard capacity. Updates that
record bytes already on disk (a sealed layer, a committed checkpoint's size) are
always accepted, because the bytes exist either way; the node then refuses new
work until relief frees space.

The node's hard capacity is the disk left after the image store, swap, the
storage cache and `sandbox.direct_disk_headroom_mb`. The storage daemon and the
memory store still check their own subtotals and feed the heartbeat's
`storage_hard_reserved_mb`.

### Workspace grants

Workspace grants apply when `sandbox.direct_split_memory_backing` is on. A
workspace keeps its ublk device at the full `disk_mb` (the ceiling), but its XFS
filesystem is created smaller, at the **grant**, and grown online as the guest
fills it.

- **Initial grant**: `min(disk_mb, sandbox.direct_workspace_initial_grant_mb)`,
  512 MiB by default. The minimum is 512 MiB (xfsprogs refuses filesystems under
  300 MB). `0` formats full-size workspaces and disables growth.
- **Why the grant bounds physical bytes**: the overlaybd upper stores only blocks
  the filesystem writes, and XFS never writes past its data section. Deleted
  blocks are not trimmed while running, so the grant, not current usage, is the
  bound.
- **Charge**: `grant + local sealed layer bytes`, with layers measured as
  allocated blocks whenever the layer set changes (seal, compaction, publish,
  import). A published workspace is charged nothing; a remount charges the grant
  again.
- **Growth**: every 0.5 s the agent `statvfs`es each mounted workspace. When free
  space falls below `max(256 MiB, size / 4)` it grows by `max(512 MiB, size / 2)`,
  capped at the ceiling. The registry reserves the larger claim first; if the
  node lacks headroom the filesystem keeps its size and the owner is retried
  after 5 s. The storage daemon journals the new grant, then runs
  `xfs_growfs -D`. A sandbox whose lifecycle lock is busy (for example, parking)
  is skipped.
- **Recovery**: the grant is journalled before growing, so the charge never
  trails the filesystem. After every mount the daemon reads the grant back from
  the XFS superblock, so imports and interrupted growth correct themselves.
- **Trim before seal**: before a park seals the upper, the storage daemon runs
  FITRIM (1 MiB minimum extents) if the upper holds at least
  `--trim-before-seal-bytes` (256 MiB) more than the filesystem uses. The guest
  is already paused. Without this a sealed layer would keep every block the
  guest ever wrote.

**Accepted risk.** A guest that writes faster than about
`free space / (poll interval + growth latency)`, roughly 500 MB/s sustained
with the defaults, or that grows on a node with no headroom left, gets `ENOSPC`
before reaching `disk_mb`. Only that guest sees it. Refusals are counted in
node metrics (`storage_workspace_growth_refusals`).

### Memory checkpoint claims

When RAM backing is on and reflink restore is off, the memory claim follows the
sandbox's state. A running sandbox's memory is on tmpfs, and its disk
allocation directory holds only an ownership marker.

| State | Memory claim (and XFS project `bhard`) |
|---|---|
| Created, running | 64 MiB |
| Park admitted | `R`, an upper bound of the capture (below) |
| Capture committed | the checkpoint's allocated bytes plus 1 MiB |
| Capture failed or rolled back | back to 64 MiB |
| Woken, checkpoint deleted | back to 64 MiB, after the next FITRIM pass |
| Deleted | released |

`R = memory file + min(resident, memory_mb) + filestore + 64 MiB`:

- the tmpfs memory file's allocated bytes, known exactly;
- the Sentry's other private pages, bounded by the cgroup's resident memory;
- the gVisor filestore (the guest's rootfs writes, including `/workspace`
  contents held there), measured exactly at park.

`R` must be an upper bound, not an estimate: a hibernate capture frees runtime
state as it writes, so running out of space part-way can lose the sandbox. If
resident memory is unknown, the fixed formula stands in for the first two
terms.

Reflink-restore owners and file-backed workers keep the fixed formula
(`2 × memory + 64 MiB`) for the sandbox's lifetime; dynamic file-backed
registrations add the filestore at park. Lowering any claim after bytes are
deleted waits for FITRIM, so freed extents in the sparse image reach the
parent disk before the claim is released. Imported and upgraded registrations
start with a fixed claim and adopt the dynamic one at their first workspace sync
or park.

### What the gateway charges

Gateway placement still requires a request's full disk to fit the node's free
hard capacity, which keeps a ceiling-sized margin for growth. For in-flight
creates the heartbeat has not yet matched, it charges only the initial claim the
node advertises (`storage_workspace_grant_mb`, `storage_memory_idle_claim_mb`),
so a create burst does not exhaust the node on paper.

### Relief, in order

1. Evict published-local cache entries (every heartbeat).
2. Publish parked sandboxes in the background, dropping their workspace claim.
3. Cold-offload published parks at 90% `storage_hard_reserved` (gateway).
4. Refuse: creates go elsewhere or queue; parks, wakes and growth are refused
   with retryable errors.

### Example: 1 GiB memory, 4 GiB disk

| State | Fixed claim | Demonstrated claim |
|---|---:|---:|
| Running, fresh | 7,232 MiB | 576 MiB (512 MiB grant + 64 MiB) |
| Parked, unpublished | 7,232 MiB | grant + layers + checkpoint, about 1.5 GiB |
| Parked, published | 3,136 MiB | checkpoint only, about 0.35 GiB |

With demonstrated claims, RAM rather than disk limits how many sandboxes run on
a node.

## Node-local model waits

`sandbox.direct_local_model_waits` (default off; needs the pause tier) lets a
node pause and thaw a sandbox around a model call by itself, with no park or
wake through the relay or gateway. Without it, every wait costs the relay a
durable park row, a `/park` and a `/wake` through the gateway: four round trips
and about 20 PostgreSQL statements per wait. The relay side is described in
[model-relay.md](model-relay.md).

**The path.** The agent calls the relay over plaintext HTTP/1.1 on the private
network, through a `sandbox.network_relays` entry whose address is private (an
IPv4 literal, or a name whose addresses are all private when the node agent
starts). Plaintext is required: the node must tell an answer from other
traffic, and over TLS 1.3 the server's session ticket arrives after the first
request and looks like a reply. Relays at public addresses are TLS ingresses
and keep the relay-driven park and wake.

**The node side** (`ucloud_sandboxes/local_wait.py`; `runtime/noded/src/local_wait/`):

- nftables logs both directions of every private-relay flow to one NFLOG group;
  the node reads only IPv4 and TCP headers, never payloads;
- candidates are owned, parkable, managed-process sandboxes with a network
  lease;
- the node pauses a sandbox when its last relay payload went out at least 50 ms
  ago and its cgroup used at most 2 ms of CPU over the last 50 ms. The pause goes
  through the pause tier, so reclaim, eviction order, escalation and thaw
  prefetch apply;
- the answer's first data packet thaws the sandbox. The paused gVisor network
  stack holds the packet until the thaw, which adds about 10 ms;
- a pause records the wait in the growth ledger (`observe_managed_wait`, with a
  `local-wait-` request ID), suspending the forecast, and the thaw records the
  continuation (`resume_managed_continuation`). Thaws are not admission-gated:
  running work comes first.

**The relay side**: it treats a request from a non-loopback peer as local
(the TLS ingress proxies from loopback), delivers the answer at once, then waits
up to 4 s for the guest to acknowledge every byte (`TIOCOUTQ` reaching 0). An
acknowledged answer cancels the durable wake row written with the result. An
unacknowledged one (the sandbox was hibernated while its call was out) marks the
request reattachable, and the wake row fires through the gateway after 5 s.

| Case | What happens |
|---|---|
| Answer arrives before the pause | No pause. |
| Answer races the pause | The thaw runs right after the pause. |
| Sandbox works while its call is out | Not paused until its cgroup is idle. |
| The pause escalates to hibernation | The connection dies, the answer is never acknowledged, and the delayed wake restores the sandbox; the agent's retry reattaches to the stored answer. |
| The relay dies before checking the acknowledgment | The durable wake fires after 5 s; waking a running sandbox does nothing. |
| An HTTP/2 client | Its PINGs read as replies: extra thaw and pause cycles, never a wrong pause. |

With the switch on, the relay sends no park for a private caller. A worker
without the node side would keep such sandboxes resident through their waits, so
the switch is rolled out with a full worker replacement. A node that outlives a
change of a relay's address keeps routing to it but stops pausing on its waits
until the node agent restarts.

## Managed-process jobs

Job status, logs, start and signal for managed-process sandboxes are served
without entering the guest (`runtime/noded/src/jobs/`). Before this, each status
read took the sandbox's locks, thawed a paused sandbox and ran
`runsc exec … ctl`; at about 200 agents per worker those reads saturated the
Python agent's GIL.

**Host-visible state.** The supervisor's state directory `/.ucloud-managed` is a
bind mount of `<bundle>/rootfs/.ucloud-managed`, the host overlay's own
directory (root-owned, mode 0700, created before `runsc create` or `restore`).
Guest writes (`state.json` by atomic rename, `stdout.log`, `stderr.log`) land in
the sandbox's overlay upper, which is already sealed at park, shipped by moves,
removed on delete and excluded from commits. A sandbox whose bundle
`config.json` lacks this mount is **legacy**, and its job routes go to the
Python agent.

**Reads.** With `--rust-create` (the noded fronts), noded serves, for owned,
running (including paused), non-legacy sandboxes:

- `GET /v1/sandboxes/{id}/jobs/{job}[?wait_ms=W&after=C]`: the job record, plus
  `cursor` = `<sequence>.<stdout_bytes>.<stderr_bytes>`. With `wait_ms`
  (clamped to 0–25,000) it answers when the cursor differs from `after`, the job
  is terminal, or `W` elapses.
- `GET /v1/sandboxes/{id}/jobs/{job}/logs/{stdout|stderr}?offset=O&limit=L[&wait_ms=W]`:
  `stream`, `offset`, `next_offset`, base64 `data`, `eof`, plus the `job`
  record. `limit` is capped at 1 MiB.

Reads never enter, thaw or lock the guest, and they do not count as sandbox
activity (so status polling does not hold off idle parking). Held reads share
one inotify instance. Files are opened with `O_NOFOLLOW` at every step and must
be regular; a symlink, FIFO or oversize file answers 409
`managed_process_state_invalid`. While a job runs, its byte counts are the log
files' sizes; once terminal, the persisted values. Parked and transitioning
sandboxes are forwarded and answered from the gateway's cache.

**Exact at park.** After a park's checkpoint, before the overlay is unmounted,
the agent derives the job record from the same files and keeps it as
`<bundle>/.ucloud-managed-process.json`. The parked node record carries it as
`managed_process` in sandbox listings, park responses and heartbeat inventory,
so the gateway's cache is exact for a parked sandbox. Parking never depends on
guest content: a missing or broken state file means no record, never a failed
park.

**Start and signal.** With `--rust-exec`, noded also serves
`POST /v1/sandboxes/{id}/jobs` and `POST /v1/sandboxes/{id}/jobs/{job}/signal`.
It validates the body as Python's `ManagedProcessStart.from_dict` does, builds
byte-identical control payloads, admits the launch through the growth ledger,
thaws the sandbox if paused (with the Rust pause tier), runs the supervisor's
`ctl` under the exec fences, and marks activity. Without the flag, or when noded
cannot run the exchange exactly as Python would, the request goes to the agent.

When the agent owns the growth ledger, noded reaches it on the agent's Unix
socket:

| Route | Body | Effect |
|---|---|---|
| `POST /internal/v1/managed/launch` | `{sandbox_id, generation, job_id, launch_sha256}` | Commits the launch intent and admits its growth before noded dispatches. 200 `{job_id}` (empty for an imported primary, which noded then binds); 503 `startup_busy`; 409 `primary_owned`. |
| `POST /internal/v1/managed/bind` | same | Binds an imported primary's identity from the supervisor's answer. |
| `POST /internal/v1/managed/terminal` | `{sandbox_id, generation, job_id}` | noded's first observation of a terminal record retires the forecast. |

Each answers 409 `generation_mismatch` for another incarnation and 409
`not_managed_process` for a sandbox not in managed-process mode. The client side
(SDK `follow()` and long-polling `wait()`) is in [api-reference.md](api-reference.md).

## Python agent and ucloud-noded

Each `sandbox.direct_node_*` flag moves one part of the node into `ucloud-noded`.
All default to `false` in `ucloud_sandboxes/config.py`, and each requires the
ones before it. With a flag off, the agent runs that part exactly as before.

| Flag | noded flag | Moves to noded | Requires |
|---|---|---|---|
| `direct_node_front_door` | (listens on the node port) | The node's HTTP port. The agent moves to a Unix socket; noded proxies every route and holds long-polls and exec streams. | — |
| `direct_node_rust_create` | `--rust-create` | Ownership of the node registry (`direct-registry.sqlite`) and the create pipeline. The agent becomes a foreign SQLite writer with cached reads. Managed-process job reads. Create admission stays the agent's, through `/internal/v1/creates/admit`. | front door |
| `direct_node_rust_exec` | `--rust-exec` | Execs on running, unpaused sandboxes, and managed-process start and signal. Fences are shared with the agent through flock files. | front door, Rust creates |
| `direct_node_rust_pause` | `--rust-pause` | The pause tier: pause and thaw, idle pauses, node-local waits, paused reclaim, escalation decisions, thaw-on-exec. Escalation calls the agent's `POST /internal/v1/pauses/escalate`, which hibernates through the durable park. | pause tier, Rust execs |
| `direct_node_rust_admission` | `--rust-admission --internal-unix <path>` | The transition ledger, the startup, restore and publication pools, managed growth forecasts, and resident sampling of managed sandboxes. | front door, Rust creates, Rust execs, Rust pause tier |

What stays in the Python agent regardless: park, wake, publication and
migration execution; lifecycle locks; restore cost quoting (it needs
authenticated checkpoint metadata); heartbeat assembly and sending; workspace
growth; and the legacy managed-process handlers.

**Cross-process state.** Without Rust admission, the two processes exchange:

| Item | Writer | Reader |
|---|---|---|
| `<state_root>/noded/agent-demand.json` (reclaim demand from the ledger) | agent | noded's pause policy |
| `<state_root>/noded/status.json` (pause-tier status for heartbeats) | noded | agent |
| `POST /internal/v1/growth/events` (local-wait waits and continuations, batched every 50 ms) | noded | agent |

**With `direct_node_rust_admission` on** (`ucloud_sandboxes/admission_backend.py`,
`runtime/noded/src/admission/`):

- The agent's `DirectSandboxService.admission` is `NodedAdmission`, an RPC client
  for noded's internal Unix socket (mode 0600, the node control token, and a
  peer-UID check). Every agent call site (create, restore, capture observation,
  publication, delete, drain close and open, continuation, launch, growth
  events, snapshots) goes through it. Refusals become the same exceptions, so
  client answers are byte-identical.
- noded admits its own creates in process and passes the claim to the agent's
  `creates/admit`, releasing it only after the create's finish returns, so a
  drain never sees an admitted create missing from the snapshot.
- noded samples managed sandboxes and publishes `<state_root>/noded/resident.json`;
  the agent's remaining readers use it. `agent-demand.json` is no longer used;
  noded's own ledger is the reclaim demand.
- Sessions: the agent says `hello` at start and whenever noded's session changes.
  A new agent session drops the old session's claims; the hello reasserts every
  claim the agent believes it holds. After a noded restart, noded refuses grants
  with a retryable 503 until that hello, or until 2 s pass with the agent
  unreachable. A request that got no answer becomes the safe retry
  (`node_startup_busy` or `node_restore_busy`).
- Heartbeats read noded's transient snapshot before the durable inventory; a
  changed noded session or a recovering noded skips that heartbeat sample rather
  than publishing zero reservations.

The registry schema (v9) is the same either way, so turning the flag off and
restarting both units rolls back.

## Configuration reference

All keys are in the deployment config's `sandbox` section unless noted.

| Key | Default | Effect |
|---|---|---|
| `policy.create_target_concurrency_per_node` | 8 | Startup (create) slots per node. |
| `direct_max_concurrent_restores` | 8 | Restore (wake) slots per node. |
| `direct_idle_park_seconds` | 0.0 | Idle time before a non-managed sandbox is paused (pause tier) or parked. 0 disables. |
| `direct_split_memory_backing` | false | Separate workspace volumes from memory backing on one project-quota XFS filesystem. Sandbox workers only; needs registry checkpoint publication. Also enables workspace grants. |
| `direct_ram_memory_backing` | false | Live application memory on a bounded tmpfs. Requires split backing. Enables demonstrated memory claims unless reflink restore is on. |
| `direct_reflink_memory_restore` | false | Restore file-backed memory from a private XFS clone; owners become file-backed at their first parked restore and keep the formula memory claim. Requires split backing. |
| `direct_workspace_initial_grant_mb` | 512 | Initial workspace filesystem size; grows online toward `disk_mb`. 0 or at least 512. Only with split backing. |
| `direct_disk_headroom_mb` | 16384 | Disk kept out of the node's hard capacity. |
| `swap_gb` | 96 | Swapfile size; also subtracted from hard disk capacity. The pause tier needs at least 1. |
| `direct_pause_tier` | false | Pause idle sandboxes and model waits in place; reclaim to swap under pressure. Requires `swap_gb` ≥ 1. |
| `direct_pause_tier_zswap` | false | zswap ahead of swap, capped at 25% of each paused cgroup's memory bound. Requires the pause tier. |
| `direct_local_model_waits` | false | Node-local pause and thaw around private-relay model calls. Requires the pause tier. |
| `network_relays` | `{}` | Relay endpoints reachable from sandboxes; private ones carry node-local waits. |
| `storage_native_max_concurrent_publications` | 4 | Concurrent snapshot publications in the storage daemon. |
| `direct_node_front_door` | false | noded owns the node port. |
| `direct_node_rust_create` | false | noded owns the registry and creates. Requires the front door. |
| `direct_node_rust_exec` | false | noded runs execs and managed-process start and signal. Requires the front door and Rust creates. |
| `direct_node_rust_pause` | false | noded owns the pause tier and local waits. Requires the pause tier and Rust execs. |
| `direct_node_rust_admission` | false | noded owns admission, growth and resident sampling. Requires the four flags above it. |

Fixed values (code constants, not configuration):

| Value | Constant | Where |
|---|---|---|
| Physical memory floor | 2048 MiB | `PHYSICAL_MEMORY_FLOOR_MB`, `resource_admission.py` |
| Admission wait | 30 s | `admission_wait_seconds`, `direct_service.py` |
| Memory-wait resample | 0.21 s | `RETRY_SECONDS`, `admission_decision.py` |
| Growth observation age | 30 s | `_GROWTH_OBSERVATION_MAX_AGE_SECONDS`, `direct_service.py` |
| Publication lanes | 16 | `_publication_slots`, `direct_service.py` |
| Hibernate break-even | 300 s | `TRANSFER_BREAK_EVEN_SECONDS`, `pause_tier.py` |
| Reclaim budget | 2 in flight, 512 MiB/s shared, 128 MiB windows | `pause_tier.py` |
| Stall threshold and backoff | 16 MiB; 10 s doubling; escalate after 2 | `pause_tier.py` |
| Swap reserve | 10% of swap | `SWAP_RESERVE_FRACTION`, `pause_tier.py` |
| Thaw prefetch | at most 1 GiB or 2 s | `PREFETCH_MAX_BYTES`, `PREFETCH_SECONDS`, `pause_tier.py` |
| Local-wait idle test | 50 ms settle, ≤ 2 ms CPU in 50 ms | `local_wait.py` |
| Idle memory claim, capture overhead | 64 MiB each | `disk_claims.py` |
| Workspace growth | poll 0.5 s; grow below `max(256 MiB, size/4)` free by `max(512 MiB, size/2)` | `disk_claims.py`, `direct_service.py` |
| Trim before seal | 256 MiB | `--trim-before-seal-bytes`, storage daemon |
