# Architecture decisions

This log records the decisions that still shape the system: what was chosen, what was
rejected, and the measurement that settled it. It also records directions that were
tried and abandoned, so nobody retries them without new evidence. Entries are grouped
by area; how the system works today is in [architecture.md](architecture.md), and what
comes next is in [roadmap.md](roadmap.md).

Each entry ends with the full record, which lives in git history. Run the
`git show` command it gives to read it.

## Isolation and runtime

### Sandboxes run under gVisor inside provider VMs, not in microVMs

The sandbox boundary is a pinned, patched gVisor (`runsc`, systrap platform) running
inside an ordinary cloud VM, so there are two layers of isolation. The UCloud VM
products that were tested expose no `/dev/kvm`, so Firecracker-style microVMs and nested
VMs would need dedicated hardware. gVisor's user-space kernel owns all guest state, so
checkpoint and restore need neither CRIU nor a VMM. An idle sandbox costs about
14 MiB of cgroup memory, and image pages are already shared between sandboxes. The
cost is a small patch series (hibernation, memory backing, paused restore) that has to
be ported on every gVisor upgrade and requalified with crash-recovery and persistence
tests.

Full record: `git show a04c1ad:docs/rl-scale-architecture-plan.md` (§2, §8, §9 S2),
`git show a04c1ad:docs/reviews/sandbox-compatibility-followup-2026-09-05.md`,
`git show a04c1ad:docs/reviews/gvisor-integration-2026-09-05.md`

### One direct-runsc Warden per node owns every sandbox; Docker never does

Each node runs one privileged Warden that calls `runsc` directly and owns every
sandbox's lifecycle on that node. Docker and containerd are image infrastructure only.
The legacy Docker task runtime, the async compatibility node agent and the
flattened-export rootfs pipeline were deleted, because each one was a second
implementation of the same lifecycle with diverging exec and routing behaviour.

Full record: `git show a04c1ad:docs/greenfield-simplification-review.md`,
`git show a04c1ad:docs/simplification-roadmap.md`

### Tasks that need unsupported kernel features get a profile, not a weaker boundary

Root-oriented benchmarks use the `linux_host` profile, which keeps the same outer
runtime, admission and network policy. A plain Docker/runc switch on shared workers was
rejected because it changes the isolation boundary. A dedicated-VM backend for tasks
that need a real kernel was proposed but not built. The POSIX ACL failure that
motivated it was fixed by porting the patch series to gVisor 20260817, not by relaxing
isolation. See [linux-environments.md](linux-environments.md).

Full record: `git show a04c1ad:docs/reviews/sandbox-qualification-2026-09-05.md`,
`git show a04c1ad:docs/reviews/sandbox-compatibility-followup-2026-09-05.md`

## Control plane and state

### PostgreSQL is the shared authority for the relay and routing; workers keep SQLite

Relay state moved to PostgreSQL on 2026-09-22, and routing and placement followed on
2026-09-25. The trigger was single-writer SQLite on the gateway. One wake waited
32.1 s for the gateway's placement process lock while holding it took 8.8 s, and
selected requests waited over 6 s for placement while their restore took 0.6 s. The
qualification slice cut burst result p95 from 3.0–3.3 s to 1.0 s at 512 results. At
steady arrival SQLite was slightly faster (0.620 s against 0.644 s), so the case is
concurrency across processes, not raw speed. Worker SQLite journals stay: they give
atomic local recovery without a network service. Redis, a separate broker and
embedded KV stores were rejected. Each adds a second delivery and recovery boundary,
or a weaker durability contract, and no measured bottleneck required one.

Full record: `git show a04c1ad:docs/shared-control-plane-design.md`,
`git show a04c1ad:docs/reviews/gateway-database-scaling-2026-09-21.md`,
`git show a04c1ad:docs/benchmarks/shared-control-2026-09-21/README.md`,
`git show a04c1ad:docs/placement-authority.md`

### A relay result is acknowledged when it is durable, not when it is delivered

A model result and its wake intent commit together in one PostgreSQL transaction. The
HTTP acknowledgement reports durable acceptance, and pending delivery is recovered
independently. Transport makes one fenced attempt and returns a typed deferral, and the
database owns retry timing, so no process holds a sleeping retry loop. The live SQLite
relay and its second retry controller were deleted in 0.5.114.

Full record: `git show a04c1ad:docs/reviews/relay-postgres-retirement-2026-09-23.md`,
`git show a04c1ad:docs/performance-architecture-plan.md` (P1)

### Heartbeats observe state; only durable transitions change ownership

Every mutation carries a positive generation, an operation id and a spec digest. A
heartbeat can confirm an existing route incarnation, but it cannot create one, move it
or raise its generation. This closed real bugs: inventory reports could replace a
route, and node-list reconciliation could create a route the gateway never allocated.
The full protocol is in [state-protocol.md](state-protocol.md).

Full record: `git show a04c1ad:docs/greenfield-simplification-review.md`,
`git show a04c1ad:PERFORMANCE_CORRECTNESS_FIXES.md`

### Silence is never loss

A late heartbeat, a failed probe or a provider suspension makes a worker
unschedulable. It never declares a sandbox lost or stops an occupied VM. A new
authenticated boot epoch proves that the old boot's processes are gone, but complete
local parks survive. Stopping a VM needs a proof: a drain acknowledgement of an empty
worker, or a lease-expired worker that never reported. Provider status alone does not
authorize termination.

Full record: `git show a04c1ad:docs/node-failure-semantics.md`

### Creates use power-of-k placement in every API process, and the node admits

This is the direction for creates; in code it is opt-in with
`gateway_create_placement: "power_of_k"` (the default is still `ranked`, see
[roadmap.md](roadmap.md)). Each gateway API process samples k = 3 capable nodes from shared heartbeats and keeps
its own in-flight overlay. It offers the create with a short admission wait, and gives
the last candidate the node's full wait. The node is the final admission authority.
This replaced ranked placement through one placement process, which ran at most 32
creates at a time across the whole fleet. Warm 512-sandbox bursts went from
19/36/44 s to 10.1/17.9/19.5 s (p50/p95/max). A choice must hold its worker in the
overlay before its database write. Without that, concurrent group plans all saw the
same idle worker, and `pack` put 504 of 512 sandboxes on one of three workers. Wakes
still go through the placement service.

Full record: `git show a04c1ad:docs/c43-placement-wiring-plan.md`,
`git show a04c1ad:docs/rl-scale-architecture-plan.md` (C4.3, ledger 2026-10-05)

### Group create packs a task's rollouts onto one node by default

verifiers-ucloud sends each task's rollouts as one group create in `pack` mode, so the
image attaches and fetches once per node. In a paired A/B at 512 sandboxes, `pack`,
`spread` and single creates were within run-to-run noise (p95 14–20 s). `pack` was
kept because it does less work per node.

Full record: `git show a04c1ad:docs/c43-placement-wiring-plan.md` ("Production canary")

### Exec traffic routes by a signed session, without a database read

On exec start the gateway issues an HMAC-signed session prefix. Later exec calls route
by heartbeat, with no PostgreSQL read and no per-exec SERIALIZABLE upsert. The route
table is read only when a worker is stale or absent, which keeps the lost, deleted and
silent answers exact. The fuller plan of client-held route tokens and a non-Python
ingress has not been built.

Full record: `git show a04c1ad:docs/rl-scale-architecture-plan.md` (C4.1, C4.2)

## Node runtime

### Node work moves into a Rust daemon, one subsystem at a time

`ucloud-noded` (Rust, tokio) took over the node's HTTP front door first, then creates,
execs, pause and local waits, and later managed-process jobs. It owns the node
registry, and the Python agent writes through it. The cause was the Python agent's
GIL. Under a burst, 10–15 threads waited for it at 15,000–22,000 re-acquisitions per
second, and doubling startup slots from 32 to 64 just stretched every phase while CPU
sat near 50%. With the daemon, 1,024 rollouts over 128 images were all ready in 27.6 s
on three workers, and exec reached 1,239–1,306 starts/s per node. A rewrite in one step
was rejected because the node code encodes hard-won crash and fencing invariants. Go
was the alternative; Rust was chosen for no GC pauses and for shared state across
threads. The gateway stays Python because it is not the bound. Children are started
with `posix_spawn`: a `pre_exec` hook had turned every exec into a full fork of the
daemon, which cost 74 ms of CPU each.

Full record: `git show a04c1ad:docs/rust-node-daemon-plan.md`

### The in-guest exec agent was built but not wired in (abandoned)

A Go agent inside the guest (C5.1) was meant to replace a host `runsc exec` per
command. Its connection direction was settled by experiment. A guest listener on a
host socket (`--host-uds=create`) makes checkpoint panic the Sentry. A guest that
dials a Warden-owned listener (`--host-uds=open`) checkpoints and restores fine. It
was never wired in, because the Rust daemon met the exec target (≥ 1,000 starts/s
per node) with plain `runsc exec`. The code and protocol remain; reopen it only for
binary-safe streams or per-exec CPU.

Full record: `git show a04c1ad:docs/guest-agent-protocol.md`,
`git show a04c1ad:docs/rust-node-daemon-plan.md` ("Exec throughput per node")

### Managed-process status and logs are read from host-visible state

Job status and log reads used to enter the guest with `runsc exec … ctl` on the Python
agent. At about 3 polls per second per sandbox, that produced gateway 504s under
600 sandboxes. Now the managed directory is a host-visible bind mount, and the daemon
serves status and logs from it, with long-polls on inotify. Clients follow a job
instead of polling it. Do not reintroduce guest-entering reads or fixed-interval
polling.

Full record: `git show a04c1ad:docs/managed-process-reads.md`

## Memory, model waits and disk

### Model waits pause the sandbox; hibernation is for pressure, drain and long waits

A sandbox waiting on a model is paused in place (`runsc pause`, 9–26 ms; resume
16–70 ms). Its memory is reclaimed to swap only under pressure, in 128 MiB windows,
idlest waits first, followed by a mandatory 8-thread prefetch on thaw. Before this, the
only answer to memory pressure was a full hibernate. At 140 rollouts with 1.5 GiB heaps
on one node, pausing cut wake p95 from 14.2 s to 0.13 s with nothing hibernated, and
it has been on in production since 0.8.5 (64 GiB swap per worker). zswap is off. On
incompressible guest memory, a zswap pool charged to the same cgroup freed only about
13 MB per reclaim. Where zswap is used, its bound is fixed (`memory.zswap.max` at 25%
of the memory bound), not toggled at runtime. A full checkpoint copies every byte the
guest has written (48–96 s after 2.5 GB of writes), which is why it is not the default
for waits.

Full record: `git show a04c1ad:docs/rl-scale-architecture-plan.md` (§9 qualification
results, ledger 2026-10-03), `git show a04c1ad:docs/rollout-0.8.0.md` ("0.8.5"),
`git show a04c1ad:docs/benchmarks/pause-reclaim-2026-10-03/README.md`,
`git show a04c1ad:docs/benchmarks/memory-pressure-2026-10-03/README.md`

### The node detects model waits itself, from TCP headers

Agents call the relay over plaintext HTTP on the private network. The node watches
only the TCP headers of relay flows (nftables NFLOG, never payloads). It pauses a
sandbox whose call is outstanding and idle, and the answer's first packet thaws it.
The relay goes through the gateway only when the guest has not acknowledged an
answer, meaning the sandbox was hibernated mid-call. This removed four round trips
and about 20 PostgreSQL statements per wait. Answer-to-agent fell from 38 ms to 22 ms
median, and a 1.3 s final-turn hold disappeared. The client must point the guest at the
relay's private name (`guest_relay_url`), or waits are never seen. See
[model-relay.md](model-relay.md).

Full record: `git show a04c1ad:docs/node-local-model-waits.md`,
`git show a04c1ad:docs/benchmarks/node-local-wake-2026-10-03/README.md`

### Live guest memory is RAM-backed and separate from the workspace volume

Guest application memory lives in RAM-backed files on the worker's XFS. It is not
coupled to the sandbox's workspace block volume. A park publishes the memory
checkpoint as its own component. The coupled layout wrote memory back continuously:
the split cut physical writes by 86.7–87.8% and raised SQLite commit throughput by
9–14%. A plain disk-file split was rejected, because it raised writes about 6.4× on
compressible heaps. One lesson stays in admission: free host RAM does not prove that
a bounded tmpfs can take more pages. Sixteen agents died of SIGBUS at 256 × 1.5 GiB,
so backing capacity is observed on its own.

Full record: `git show a04c1ad:docs/benchmarks/split-memory-2026-09-23/README.md`,
`git show a04c1ad:docs/reviews/hybrid-memory-pressure-2026-09-23.md`,
`git show a04c1ad:docs/benchmarks/pressure256-2026-09-23/README.md`,
`git show a04c1ad:docs/split-memory-backing-rollout.md`

### Disk is an exact reservation; CPU and memory are admitted from live pressure

Every placement, live or parked, reserves its full disk, and committed bytes are never
silently overcommitted. CPU and memory requests must fit the node shape. Beyond that,
they are admitted from actual CPU, available memory and PSI rather than summed. In
practice disk claims, not RAM, bound density. A 540-sandbox ramp on three workers
stopped at 180 per node with 89.8% of disk reserved and 59–82 GB of RAM free.
Collapsing writable state onto one node-local filesystem is the planned way to lift
that bound. See [node-admission-and-memory.md](node-admission-and-memory.md).

Full record: `git show a04c1ad:docs/benchmarks/density-rc38-2026-09-26/README.md`,
`git show a04c1ad:docs/rl-scale-architecture-plan.md` (D1, §8)

### Cold published parks are offloaded to free disk; workers are not kept for them

When hard disk reservations on a worker reach 90%, the autoscaler detaches parks that
are already published until it reaches 80%. It also does this when a queued create
needs the space. Candidates are ranked by released reservation per published byte. It
reuses the gateway's existing detach protocol, with an exact generation and snapshot
fence rechecked in the routing transaction, so there is no second eviction path. A
detached park wakes cold on any worker.

Full record: `git show a04c1ad:docs/reviews/cold-offload-policy-2026-09-23.md`

### Direct I/O on the storage upper was rejected

Opening upper-data files with `O_DIRECT` passed every correctness and migration test.
Performance collapsed: random mixed IOPS fell from 169,732 to 9,341, and sequential
bandwidth from 1.44 GB/s to 0.34 GB/s. It was never installed.

Full record: `git show a04c1ad:docs/benchmarks/direct-upper-rejected-2026-09-23/README.md`

## Images

### Images are stored once per 256 KiB chunk, deduplicated by content

Every image is kept as content-addressed 256 KiB chunks, keyed by the SHA-256 of the
uncompressed bytes and packed into immutable packs. A global index serves builders
and GC, and each image carries a signed chunk map. Dedup by layer identity had failed:
two ScaleSWE images with 98.55% identical file bytes shared no components. On 181
training images, the chunk store held 17.4 GB, against 149.8 GB of OCI plus EROFS
before it. The production corpus of about 8,100 images moved over in four waves
(M2). Trust is unchanged: signed roots pin the chunk maps, and every chunk is verified
after decompression. See [images.md](images.md).

Full record: `git show a04c1ad:docs/chunk-store-design.md`,
`git show a04c1ad:docs/chunk-store-m2-plan.md`,
`git show a04c1ad:docs/reviews/prepared-image-cache-2026-10-02.md`

### Images are Nydus RAFS v6, mounted by kernel EROFS; Nydus's chunk dictionary is not used

Images are converted per layer with `nydus-image`, with no chunk dictionary. Workers
mount the RAFS v6 bootstrap with the kernel's EROFS driver over an NBD device. One
device per image attached in 0.18–0.24 s, against 0.23–0.79 s for the earlier 2–8
EROFS components. A cold `import sys` read 8–12 MB instead of 31–56 MB. RAFS also
keeps file mtimes: the earlier `mkfs.erofs -T 0` zeroed them, which made Python
recompile every `.pyc` (3.74 s against 0.9 s for a scientific import). The chunk
dictionary was rejected for two reasons. It is capped at 254 blobs and panicked at
image 158 of 181. Dictionaries grown with `merge` also produced corrupt images: one
had 2,076 files with wrong bytes.

Full record: `git show a04c1ad:docs/benchmarks/nydus-spike-2026-10-02/README.md`

### Stock nydusd serves the image device, not a reader of our own

The worker's image device is upstream nydusd v2.4.5, built with `block-nbd` and
pinned by digest. A Python NBD reader and a planned custom Rust daemon (C2.1) were
both dropped. The Python reader capped a node at about 45 MB/s, because one 256 KiB
miss per request ran into a request-rate ceiling. On 64 cold images, nydusd with a
shared node cache and 8 attach slots took 10.6 s against 31.5 s. Its trust holds end
to end: the signed bootstrap pins a TOC, and nydusd checks each chunk it fetches. It
fails closed: killing a daemon gives EIO only for that daemon's images. Parallel
attach had made the Python reader 7× slower, so attach concurrency rose to 8 only
together with nydusd.

Full record: `git show a04c1ad:docs/benchmarks/nydusd-spike-2026-10-03/README.md`,
`git show a04c1ad:docs/benchmarks/attach-spike-2026-10-03/README.md`

### Kernel on-demand paths (fscache, fanotify) were not adopted

EROFS over fscache was about 2× faster in the spike, but it was rejected. The option is
deprecated upstream and scheduled for removal. It needs unsigned out-of-tree modules,
because Ubuntu does not enable it. A plain daemon restart breaks every mount. Most of
its speed came from compression, larger fetches and concurrent attach, which nydusd
over NBD also provides. fanotify pre-content hooks with file-backed EROFS were correct
but not adopted. When the listener dies, the kernel returns zeros instead of an error,
whereas NBD fails with EIO in 45 ms. Revisit either only if upstream support settles
and the path can fail closed.

Full record: `git show a04c1ad:docs/benchmarks/fscache-spike-2026-10-02/README.md`,
`git show a04c1ad:docs/benchmarks/fanotify-spike-2026-10-02/README.md`,
`git show a04c1ad:docs/chunk-store-design.md` ("Decisions (2026-10-02)")

### Sentry-native EROFS mounts were demoted to an optional cleanup

Mounting EROFS inside the gVisor Sentry was planned as a density lever, on the
assumption that the gofer path duplicates image pages per sandbox. Measurement showed
the pages are already mostly shared. After a scientific-stack import, private memory
per sandbox was +15 MiB through the gofer and +12 MiB Sentry-native. The memory lever
is the pause tier instead.

Full record: `git show a04c1ad:docs/rl-scale-architecture-plan.md` (§9 S2),
`git show a04c1ad:docs/benchmarks/rl-scale-spikes-2026-10-01/README.md`

### S3 is the durable store; workers read a store node, never S3

Packs, bootstraps and chunk maps live in S3, which is cheap and unbounded. Workers do
not read it. S3's tail failed the demand-read gate: 1 MiB GETs at concurrency 32 had a
p99 of 5.5 s against a 150 ms gate, and private-only workers would reach it through the
gateway's NAT. Workers instead range-read a store node on the private network. It holds
a full replica of the S3 prefix, hosts the chunk index, and verifies every extent.
There is one store node, with a measured replacement runbook, and no second node or
S3 fallback. If it is lost, image reads stop with EIO; it never serves wrong bytes.
Resident reads are answered by a Go server on every core, because one Python process
capped reads near one core.

Full record: `git show a04c1ad:docs/chunk-store-design.md` (§2, S12),
`git show a04c1ad:docs/chunk-store-m2-plan.md` (§9),
`git show a04c1ad:docs/hetzner.md` ("Chunk store node")

### Offline image factoring on the registry Volume was retired

Before the chunk store, sharing meant manufacturing identical OCI layer prefixes
offline. That covered image pools, foundations as `FROM` rewrites, shared-task
anchors with flat deltas, compact project bases and layered task pools, all stored on
one registry Volume. The Volume grew to 4 TB, filled in production, and still covered
only 742 of 35,984 source references. Content-addressed chunks make that machinery
unnecessary. Once the corpus moved, the Volume shrank to 250 GB and was then retired.
A registry remains for park checkpoints, metadata and the build cache. On UCloud it
sits on the shared project drive, so its percentage-based disk guard is disabled. Do
not revive per-image anchor or delta schemes for sharing.

Full record: `git show a04c1ad:docs/image-pools.md`,
`git show a04c1ad:docs/shared-task-images.md`,
`git show a04c1ad:docs/chunk-store-m2-plan.md` (§5.4),
`git show a04c1ad:docs/reviews/prepared-image-cache-2026-10-02.md`

### Harnesses and tools ship as toolkits composed at the gateway

A toolkit is a signed, read-only environment under `/opt/ucloud/toolkits/<name>/`.
The gateway composes it with the image's root and signs the result, so nodes needed no
change. Composing on the node was rejected, because it would have changed the image
lease index, receipts and crash recovery for no gain. Before this, every rollout
installed `uv` and the harness from PyPI. Setup at 512 rollouts fell from p50 71.8 s to
4.2 s. Large harness bundles are linked from the toolkit rather than unpacked, which
had filled per-sandbox disks.

Full record: `git show a04c1ad:docs/toolkit-layers.md`

### Recipe images build in sandboxes on workers, straight into the chunk store

A task image is a prepared base plus a few steps. Those steps now run as a shell script
in a sandbox started from the base's chunk-store root, and the result is born in the
chunk store. Building them with Docker on builder VMs had meant regenerating each
base's OCI copy, pushing gzip layers and converting back. In the 250-task pilot the
task steps were only 6–52% of end-to-end time. OpenSWE-sized families are built on
first use, plus ensure-ahead from the trainer, instead of storing every image ahead of
time. Build concurrency stays small: the slow part was S3 tail latency, not slot
count.

Full record: `git show a04c1ad:docs/sandbox-builds.md`,
`git show a04c1ad:docs/benchmarks/build-pilot-2026-10-07/README.md`

### Training images resolve by name through the gateway's image index

verifiers sends image names, as it would to Prime, never Dockerfiles. The gateway
answers a name only if its index holds something prepared for it: a whole image, or a
recipe on a prepared base. Unknown names used to fall through to Docker Hub and fail,
or pull upstream instead of the prepared copy. The index also exports, per environment,
the task ids the trainer may sample, so tasks with nothing prepared are never drawn.

Full record: `git show a04c1ad:docs/image-index.md`,
`git show a04c1ad:docs/rl-scale-architecture-plan.md` (ledger 2026-10-09)

## Deployment and operations

### Production runs on UCloud; the chunk store's S3 stayed at Hetzner

Production ran at Hetzner from late September until 2026-10-05, then returned to UCloud:
gateway, store node and autoscaled 64-vCPU workers. The Hetzner S3 bucket was kept,
because it holds the only copy of the training corpus. The new store node refilled
its replica from it (331 GB) in under two hours. The move exposed Hetzner-only
assumptions: root logins, tools in provider images, hand-installed binaries, and
registry `host:port` baked into stored references. So node and gateway setup is
verified as the non-root service user on the stock image, and provider specifics stay
behind the compute-provider adapter. See [deployment.md](deployment.md).

Full record: `git show a04c1ad:docs/rollout-0.8.0.md` ("Production moves to UCloud"),
`git show a04c1ad:docs/hetzner.md`

### Machines are addressed by DNS name, never by private IP

UCloud VM restarts change private addresses. On one restart, every literal address in
the configuration broke: workers could not heartbeat and stored image references
stopped resolving. Configuration therefore names the gateway and store node by their
project DNS names, and services bind all interfaces. Sandboxes reach the relay and
package cache through names in their `/etc/hosts`, which map to stable virtual
addresses that the worker translates. A virtual IP in client configuration was
rejected in favour of names everywhere. See [operations-runbook.md](operations-runbook.md).

Full record: `git show a04c1ad:CHANGELOG.md` (0.9.70),
`git show a04c1ad:docs/rollout-0.8.0.md` (UCloud sections)

### Managed hosts do not take unattended package upgrades

Gateway, worker, builder and snapshot preparation disable unattended APT upgrades and
mask their timers. Package updates are explicit maintenance operations. This keeps a
library update from restarting PostgreSQL, the registry or control-plane services in
the middle of a run.

Full record: `git show a04c1ad:docs/hetzner.md` ("OS update policy")

## Engineering practice

### Contracts are strict and single-spelling; there are no compatibility shims

Configuration and wire payloads reject unknown fields and contradictory input, and
each concept has one spelling. Durable-state conversion runs as explicit, removable
migration tools, never during startup. Scheduling requires exact capabilities, not a
version floor. Simplification is measured by representations, modes and mechanisms
deleted; moving code between files does not count. Package and test line-count budgets
were tried and dropped (2026-10-06), because they stopped nothing and cost a note on
every change. Separate journals stay where they mark real writer or privilege
boundaries.

Full record: `git show a04c1ad:docs/greenfield-simplification-review.md`,
`git show a04c1ad:docs/simplification-roadmap.md` ("Invariants")

### Durable logic is tested with state machines and real dependencies, not mocked choreography

Crash, replay and fencing behaviour is owned by state-machine tests, with named
regressions kept for real incidents. Broad input spaces belong to property tests.
Relay and routing tests run against real PostgreSQL, and transports against real
HTTP, registry and S3-compatible servers. Exact SQL, call counts, argv and timing are
not asserted unless an external protocol exposes them.

Full record: `git show a04c1ad:docs/testing-review-and-remediation.md`,
`git show a04c1ad:docs/reviews/relay-postgres-retirement-2026-09-23.md`
