# Architecture decisions

Decisions a newcomer might question or accidentally undo, and directions that were
tried and abandoned. How the system works today is in [architecture.md](architecture.md)
and [images.md](images.md); what is left is in [roadmap.md](roadmap.md). Each entry
ends with a `git show` command for the full record.

## Isolation and runtime

### Sandboxes run under gVisor inside ordinary VMs, owned by one Warden per node

The tested UCloud VMs expose no `/dev/kvm`, and gVisor owns all guest state, so
checkpoint needs neither CRIU nor a VMM (idle sandbox ~14 MiB). The price is a patch
series to port on every gVisor upgrade. Tasks needing unsupported kernel features get
the `linux_host` profile, never a Docker/runc switch that weakens the boundary. The
Warden calls `runsc` directly; the Docker task runtime and other second lifecycles
were deleted, and Docker is never a sandbox owner.

Full record: `git show a04c1ad:docs/rl-scale-architecture-plan.md` (§2, §8, §9 S2),
`git show a04c1ad:docs/reviews/sandbox-compatibility-followup-2026-09-05.md`,
`git show a04c1ad:docs/reviews/gvisor-integration-2026-09-05.md`,
`git show a04c1ad:docs/reviews/sandbox-qualification-2026-09-05.md`,
`git show a04c1ad:docs/greenfield-simplification-review.md`,
`git show a04c1ad:docs/simplification-roadmap.md`

## Control plane and state

### PostgreSQL is the shared authority; a relay result is acked when durable

Relay, routing and placement moved off single-writer gateway SQLite (one wake waited
32.1 s for a placement lock; burst p95 fell from ~3.1 s to 1.0 s at 512). A result and
its wake intent commit in one transaction, and the database owns retry timing. Workers
keep SQLite journals. Redis, a broker and embedded KV stores were rejected as a second
recovery boundary no measurement required.

Full record: `git show a04c1ad:docs/shared-control-plane-design.md`,
`git show a04c1ad:docs/reviews/gateway-database-scaling-2026-09-21.md`,
`git show a04c1ad:docs/benchmarks/shared-control-2026-09-21/README.md`,
`git show a04c1ad:docs/placement-authority.md`,
`git show a04c1ad:docs/reviews/relay-postgres-retirement-2026-09-23.md`,
`git show a04c1ad:docs/performance-architecture-plan.md` (P1)

### Heartbeats only observe, and silence is never loss

Only durable, fenced transitions change ownership; a heartbeat can confirm a route but
never create or move it (both had happened). A late heartbeat or provider suspension
only makes a worker unschedulable; stopping a VM needs a drain acknowledgement or a
never-reported expired lease.

Full record: `git show a04c1ad:docs/greenfield-simplification-review.md`,
`git show a04c1ad:PERFORMANCE_CORRECTNESS_FIXES.md`,
`git show a04c1ad:docs/node-failure-semantics.md`

### Creates use power-of-k placement and node admission; exec skips the database

The direction, but the code default is still `ranked` (`gateway_create_placement:
"power_of_k"` opts in). Each process samples k = 3 nodes and must hold its choice in an
in-flight overlay before the database write, or `pack` put 504 of 512 on one worker.
It replaced a 32-at-a-time placement process: 512-sandbox p95 went from 36 s to 17.9 s.
Groups default to `pack` (equal to `spread` within noise, less work per node). Exec
routes by a signed session prefix, reading the route table only for a stale worker.

Full record: `git show a04c1ad:docs/c43-placement-wiring-plan.md`,
`git show a04c1ad:docs/rl-scale-architecture-plan.md` (C4.1–C4.3, ledger 2026-10-05)

## Node runtime

### Node work moves into a Rust daemon one subsystem at a time

The Python agent's GIL was the bound: doubling startup slots just stretched every
phase at ~50% CPU. With `ucloud-noded`, 1,024 rollouts were ready in 27.6 s on three
workers and exec reached ~1,250 starts/s per node. A one-step rewrite was rejected
(the node code holds the crash and fencing invariants); Go lost to Rust for no GC
pauses. Children use `posix_spawn`: a `pre_exec` hook cost 74 ms of CPU per exec.
The in-guest exec agent was built but never wired in, since plain `runsc exec` met the
target; if revived, the guest must dial out (`--host-uds=create` panics checkpoint).

Full record: `git show a04c1ad:docs/rust-node-daemon-plan.md`,
`git show a04c1ad:docs/guest-agent-protocol.md`

### Managed-process status and logs come from host-visible state

Guest-entering `runsc exec … ctl` reads at ~3 polls/s per sandbox caused gateway 504s
under 600 sandboxes. Do not reintroduce guest-entering reads or fixed-interval polling.

Full record: `git show a04c1ad:docs/managed-process-reads.md`

## Memory, model waits and disk

### Model waits pause the sandbox in place, detected on the node from TCP headers

`runsc pause` takes 9–26 ms; memory goes to swap only under pressure. At 140 rollouts
this cut wake p95 from 14.2 s to 0.13 s; a full checkpoint copies every written byte
(48–96 s after 2.5 GB). The node watches relay TCP headers (NFLOG, never payloads),
saving ~20 PostgreSQL statements per wait; it needs `guest_relay_url`. zswap is off: it
freed ~13 MB per reclaim on incompressible memory.

Full record: `git show a04c1ad:docs/rl-scale-architecture-plan.md` (§9 qualification
results, ledger 2026-10-03), `git show a04c1ad:docs/rollout-0.8.0.md` ("0.8.5"),
`git show a04c1ad:docs/benchmarks/pause-reclaim-2026-10-03/README.md`,
`git show a04c1ad:docs/benchmarks/memory-pressure-2026-10-03/README.md`,
`git show a04c1ad:docs/node-local-model-waits.md`,
`git show a04c1ad:docs/benchmarks/node-local-wake-2026-10-03/README.md`

### Live guest memory is RAM-backed, separate from the workspace volume

Splitting it cut physical writes ~87%; a disk-file split raised them ~6.4×. Backing
capacity is admitted on its own: free host RAM did not prove tmpfs room (16 SIGBUS
deaths at 256 × 1.5 GiB). `O_DIRECT` on the storage upper was rejected: random IOPS
fell from 169,732 to 9,341.

Full record: `git show a04c1ad:docs/benchmarks/split-memory-2026-09-23/README.md`,
`git show a04c1ad:docs/reviews/hybrid-memory-pressure-2026-09-23.md`,
`git show a04c1ad:docs/benchmarks/pressure256-2026-09-23/README.md`,
`git show a04c1ad:docs/split-memory-backing-rollout.md`,
`git show a04c1ad:docs/benchmarks/direct-upper-rejected-2026-09-23/README.md`

### Disk is an exact reservation; cold published parks are offloaded to free it

CPU and memory are admitted from live pressure, but disk is never overcommitted, so
disk bounds density (180/node at 89.8% disk with 59–82 GB RAM free). At 90% disk the
autoscaler detaches published parks down to 80% through the existing fenced detach.

Full record: `git show a04c1ad:docs/benchmarks/density-rc38-2026-09-26/README.md`,
`git show a04c1ad:docs/rl-scale-architecture-plan.md` (D1, §8),
`git show a04c1ad:docs/reviews/cold-offload-policy-2026-09-23.md`

## Images

### Images are stored once per content-addressed 256 KiB chunk

Layer-identity dedup failed (two images 98.55% identical shared nothing). 181 training
images took 17.4 GB as chunks against 149.8 GB before. Do not revive the offline
pools, anchors and deltas on the old registry Volume: it reached 4 TB and still
covered only 742 of 35,984 references.

Full record: `git show a04c1ad:docs/chunk-store-design.md`,
`git show a04c1ad:docs/chunk-store-m2-plan.md` (§5.4),
`git show a04c1ad:docs/reviews/prepared-image-cache-2026-10-02.md`,
`git show a04c1ad:docs/image-pools.md`,
`git show a04c1ad:docs/shared-task-images.md`

### Images are Nydus RAFS v6 served by stock nydusd, without a chunk dictionary

Kernel EROFS over NBD attaches in ~0.2 s and keeps mtimes (zeroed mtimes made `.pyc`
recompile: 3.74 s against 0.9 s). The chunk dictionary panicked at 254 blobs and
`merge` corrupted images. Our Python NBD reader capped at ~45 MB/s; nydusd took 64 cold
images in 10.6 s against 31.5 s, so the planned custom Rust reader was dropped.

Full record: `git show a04c1ad:docs/benchmarks/nydus-spike-2026-10-02/README.md`,
`git show a04c1ad:docs/benchmarks/nydusd-spike-2026-10-03/README.md`,
`git show a04c1ad:docs/benchmarks/attach-spike-2026-10-03/README.md`

### fscache, fanotify and Sentry-native EROFS were not adopted

fscache is deprecated upstream and needs out-of-tree modules; fanotify returns zeros,
not EIO, when its listener dies. Sentry-native mounts saved only ~3 MiB per sandbox
because image pages are already shared. Revisit only if a path can fail closed.

Full record: `git show a04c1ad:docs/benchmarks/fscache-spike-2026-10-02/README.md`,
`git show a04c1ad:docs/benchmarks/fanotify-spike-2026-10-02/README.md`,
`git show a04c1ad:docs/chunk-store-design.md` ("Decisions (2026-10-02)"),
`git show a04c1ad:docs/benchmarks/rl-scale-spikes-2026-10-01/README.md`

### S3 is the durable store; workers read one store node, never S3

S3 1 MiB GETs had p99 5.5 s against a 150 ms gate. The store node holds a full
replica and verifies every extent; if lost, reads fail with EIO, never wrong bytes.

Full record: `git show a04c1ad:docs/chunk-store-design.md` (§2, S12),
`git show a04c1ad:docs/chunk-store-m2-plan.md` (§9),
`git show a04c1ad:docs/hetzner.md` ("Chunk store node")

### Harnesses ship as toolkits composed at the gateway, not on the node

Composing on the node would have changed leases, receipts and recovery. Setup at 512
rollouts fell from p50 71.8 s to 4.2 s. Large bundles are linked, not unpacked, since
unpacking filled per-sandbox disks.

Full record: `git show a04c1ad:docs/toolkit-layers.md`

### Images resolve by name through the gateway index; recipes build in sandboxes

Clients send names, never Dockerfiles, and the index exports the samplable task ids.
Recipe steps run in a sandbox on the base's chunk-store root, born in the chunk store,
not Docker on builder VMs. Large families build on first use plus ensure-ahead; build
concurrency stays small because S3 tail latency, not slots, was the bound.

Full record: `git show a04c1ad:docs/image-index.md`,
`git show a04c1ad:docs/rl-scale-architecture-plan.md` (ledger 2026-10-09),
`git show a04c1ad:docs/sandbox-builds.md`,
`git show a04c1ad:docs/benchmarks/build-pilot-2026-10-07/README.md`

## Deployment and operations

### Production runs on UCloud, addressed by DNS name, never by private IP

The chunk store's S3 bucket stayed at the earlier provider (the store node refilled
331 GB in under two hours). A VM restart changed every private IP, so configuration
and sandboxes use names; setup is verified as the non-root user on stock images.
Unattended upgrades are off so nothing restarts mid-run. See [operations.md](operations.md).

Full record: `git show a04c1ad:docs/rollout-0.8.0.md` ("Production moves to UCloud"),
`git show a04c1ad:docs/hetzner.md` (incl. "OS update policy"),
`git show a04c1ad:CHANGELOG.md` (0.9.70)

## Engineering practice

### Contracts are strict and single-spelling, with no compatibility shims

Unknown fields are rejected, migrations are explicit tools, and scheduling needs exact
capabilities. Line-count budgets were tried and dropped (2026-10-06).

Full record: `git show a04c1ad:docs/greenfield-simplification-review.md`,
`git show a04c1ad:docs/simplification-roadmap.md` ("Invariants")

### Tests check decisions, contracts and scenarios, not implementation or time

Crash and fencing behaviour has state-machine tests against real PostgreSQL, HTTP,
registry and S3 servers; exact SQL, call counts and timing are not asserted. On
2026-10-18 the suite (~3,100 Python tests, ~100k lines) was found to patch privates
~1,000 times, test behaviours at up to five layers, and synchronize with sleeps;
since CI was fixed, about one run in five failed on a timing race. Rather than
inject a clock or drain hooks into production code, tests now test time-dependent
logic through pure decision functions with an explicit `now`, and wait for end
states (`tests.support.wait_until`). The rules are in AGENTS.md.

Full record: `git show a04c1ad:docs/testing-review-and-remediation.md`,
`git show a04c1ad:docs/reviews/relay-postgres-retirement-2026-09-23.md`
