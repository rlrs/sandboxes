# Storage-native backend

This directory builds and qualifies the node-wide ublk/OverlayBD storage
dependency used by the storage-native runtime. AgentEnv supplies the block
device implementation; the direct Warden remains the sole sandbox lifecycle
owner.

The dependency is pinned to the AgentEnv v0.2.2 release commit
`771ea55ca80abbfacc85e716ec91c40e82b3398b` under its MIT license. This release
includes cache allocation/eviction safety, premerged-index memory fixes, and
hybrid-upper discard/rewrite allocation reuse. **Old hybrid writable uppers
cannot be reopened with this backend.** Use the migration procedure below.
Build it on Linux with a current stable Rust toolchain from
from an exact, clean checkout:

```bash
./build_pinned.sh /path/to/AgentENV /path/to/artifacts
```

The build applies the dense/compacted-stream export, pooled-exclusive-delete,
owner-identity, owner-transition, premerged-identity, device-reuse, jemalloc,
storage-upgrade-compatibility, and positioned-read-advice patches. The latter
keeps buffered I/O and caching but suppresses speculative Linux readahead on
backing files: the block backend already requests explicit byte ranges.
It does not change the writable format. The tagged upstream daemon does
not activate jemalloc; our patch restores upstream's allocator change.
It runs targeted compaction, cache, ownership, device-pool, and protocol tests and emits a
content-addressed binary, license, and schema-3 build manifest with every patch
digest and `hybrid_upper_sub_version: 2`. Both packaging and VM initialization
validate the full patch list and writable-format marker. A production package
must use that manifest and must not fetch or build
an unpinned branch during node startup.

The daemon requires both `--global-config` and
`--resize-global-config`. Production init writes separate runtime and resize
configs backed by sibling `remote-blocks` and `resize-blocks` directories.
They must remain isolated: the offline C++ resize cache has destructive
eviction semantics that are not compatible with the shared Rust runtime cache.
Background download remains disabled in both configs.

## Migrating from the v0.1.2 backend

This is a storage migration, not a live executable replacement. Read-only
sealed layers remain compatible; writable hybrid data/index pairs do not.
The local format patch stamps fresh hybrid uppers with sub-version 2 and
rejects old or unknown versions before index replay. Do not edit existing
headers to bypass this check. The old daemon has no reciprocal rejection
guard, so rollback must also use sealed layers and fresh uppers.
VM initialization also refuses to overwrite an installed backend without
current format provenance, before installing packages or restarting services.
Use a fresh base image or a newly baked image carrying this backend; cloning
an old prebuilt worker image does not make its storage format current. Do not
remove the installed binary or provenance file to bypass the guard.

1. Retain the old binary, package bundle and configuration. Quiesce each
   sandbox on the old stack, seal its upper, and complete durable snapshot
   publication before releasing its last local copy. A parked sandbox with
   unpublished layers has **not** completed migration.
2. Prepare fresh workers with the new pinned artifact and fresh cache/runtime
   directories. Restore the published immutable rootfs and memory state into
   fresh writable uppers, then verify execution and another park/resume cycle.
   Keep old workers available until all assigned state is accounted for; an
   unreachable worker or an old heartbeat does not authorize deleting it.
3. Switch placement to qualified new workers and drain old workers through
   normal ownership-fenced lifecycle operations. Never replace the daemon
   underneath mounted devices or reuse an old worker's writable directories.
4. To roll back, quiesce and publish using the new daemon, then restore those
   sealed layers with the retained old stack into **new** writable uppers.
   Do not start the old daemon against new writable state. Keep both artifacts
   until the migrated workload has passed its acceptance checks.

UCloud and Hetzner use this same storage format and require the same procedure.
No SDK change is needed for the backend upgrade.

The upgrade and sealed-layer rollback qualification, including old-upper
rejection and real gVisor processes, is recorded in
`git show a04c1ad:docs/reviews/agentenv022-upgrade-2026-09-22.md`, with the
remaining production acceptance checks.

The device-reuse patch treats the pool high watermark as a steady-state idle
cache target, not a bound on active devices or recently returned devices.
Returns stay reusable for 60 seconds; a five-second maintenance task retires
only expired surplus. Acquisitions prefer exact-size devices before resizing.
Prewarming maintains the low watermark and defers to concurrent returns;
allocation failures retain the single-flight guard during exponential backoff
from one to 32 seconds. Shutdown rejects late returns. Mount ownership,
exclusive-use checks, and quarantine remain prerequisites for safe reuse.

Sandbox VM init also sets `vm.watermark_scale_factor=100` so background reclaim
starts with more free-page headroom during buffered snapshot/rootfs I/O. This
does not cap sandbox memory or disable compaction. The isolated UCloud
qualification (`git show a04c1ad:docs/reviews/device-reuse-memory-2026-09-20.md`) records
the measured reduction in churn and compaction, including its limitations.

## Published snapshot-chain compaction

Each ordinary publication appends the newly sealed writable layer to the
existing immutable Registry chain. The node compacts the prospective chain
before publishing when it would exceed either eight layers or 4 GiB of accumulated
delta data after the oldest base layer. Local sealed layers use allocated bytes
as the size estimate, excluding sparse virtual-address holes. Excluding the base
avoids repeatedly flattening an already compacted multi-GiB snapshot after every
small update. These are compaction triggers, not snapshot size limits. They bound
lookup depth and Registry metadata without putting a
large temporary flattened file on the worker's constrained local disk.

Compaction selects a size-tiered suffix. It starts at the newest layer and
includes an older layer when the selected suffix is at least as large, or when
retaining it would exceed the depth target. This keeps both a large base and
large previously merged deltas out of repeated small merges. Exceeding the byte
trigger alone does not rewrite a single large delta. Growth eventually includes
older tiers; forcing one layer or changing blob origin still requires a full
merge. Publishers retain only already-published tiers and include every local
input in the exported suffix.

Compaction opens the selected remote-plus-local layers through AgentEnv's shared
bounded cache, flattens them with ordered writes, and streams one content-addressed
layer directly into the durable backend. Partial merges must preserve explicit
zero/discard mappings and unmapped holes, since an older base remains underneath. Publication is
transactional at the control-plane boundary: the old Registry descriptor and
local sealed delta remain authoritative until the new OCI manifest is durable.
An export, upload, or manifest failure therefore leaves a resumable attached
park rather than deleting its last valid state. Registry garbage collection can
later reclaim any unreferenced upload created by a failed attempt.

Publishers retain a bounded in-memory index of completed layer uploads, including
compacted exports. If a later layer or final metadata commit fails, a retry can
reuse those blobs without rereading or uploading their immutable inputs. Reuse
requires the same input file identities, compaction context and remote blob
presence; missing blobs are exported again. The index is scoped to the publisher
and is lost on restart. It does not retain partial uploads or make an incomplete
snapshot authoritative. `snapshot_reused_layers` and `snapshot_reused_layer_bytes`
count reused outputs; these bytes are excluded from `snapshot_uploaded_bytes`
for successful publication attempts. As before, that uploaded-byte metric does
not account for failed attempts.

The Hetzner CPX62 qualification compacted two generations into one 98.7 MB
layer in 0.868 seconds, then mounted and verified both generations in 59 ms.
That is a correctness and small-warm-chain measurement, not a cold multi-GiB
compaction or concurrent-publication SLO.

The pooled-delete patch is deliberately narrow. AgentEnv continues to own pool
acquire, target swapping, cache eviction, refill, and release. The patch only
allows an explicit `Delete` request to permanently destroy an active exclusive
pooled device after uncertain mount cleanup; it refuses shared pooled devices.
The owner-identity patch makes runtime-device acquisition idempotent and
reports active owner bindings separately from idle devices, allowing the
UCloud journal to recover an acquisition interrupted before the device id was
recorded. The owner-transition patch makes the forward/reverse ownership index
atomic, fences late release/delete completions to their captured owner, and
serializes retries only for the same owner. Registry locks never span device
I/O; unrelated acquisitions remain concurrent. Its regressions reproduce a
release completing after the pool has reassigned the same numeric device ID.

## Destructive volume qualification

Run only as root on a disposable Linux VM with kernel ublk support. The
qualifier creates and formats a ublk device, mounts filesystems and overlayfs,
freezes and seals the device, destroys it, reconstructs a new device from the
sealed layer, verifies filesystem state, and checks the requested hard device
boundary:

```bash
sudo python3 qualify_volume.py \
  --daemon /path/to/uvm-ublk-daemon-<sha256> \
  --work-root /var/lib/ucloud/storage-native-qualification \
  --output /tmp/storage-native-volume.json
```

Required host tools are `mkfs.ext4`, `mount`, `umount`, `fsfreeze`,
`fallocate`, `setfacl`, and `getfacl`. The work root must be an existing,
dedicated directory; the qualifier creates a unique child and never removes
the supplied root.

Pass `--runsc`, `--conformance-workload`, and `--noop-workload` together to
run the pinned gVisor workload as part of the same destroy/reconstruct cycle.

The other one-off benchmarks and qualifiers (I/O against native loopback XFS,
warm-device churn, delta and tiered compaction, local checkpoint compaction,
published-local cache, XFS trim, and the v0.1.2 upgrade) are in
`git show b3b951c:runtime/storage_native/`.

## Unpublished local checkpoint compaction

The node service also compacts local sealed layers independently of remote
publication. Maintenance starts when local depth exceeds eight layers or
accumulated delta data exceeds 4 GiB, configurable with
`--local-compact-after-layers` and `--local-compact-after-bytes`. These trigger
background work; they do not reject sandbox operations or guarantee an immediate
chain-depth bound. One background export runs per node, with pending requests
coalesced by volume and physical disk headroom checked during export.

A wake can continue while compaction runs. The next journaled mount or publication
adopts a completed replacement if its immutable source prefix still matches,
preserving newer deltas. Size-tiered suffix selection also applies to entirely
local chains. Existing remote layers remain
unchanged. Compaction does not make a checkpoint portable to another node.

Storage-native metrics expose `local_compaction_active`, `local_compaction_waiting`,
`local_compaction_completed`, `local_compaction_adopted`, `local_compaction_failed`,
`local_compaction_deferred`, `local_compaction_input_bytes`, and
`local_compaction_output_bytes`. Deletion cancels pending and active exports;
`local_compaction_cancelled` counts cancelled operations and
`local_compaction_discarded_bytes` measures locally written output discarded before
candidate commit. Counters reset with the service process.

See the qualification report
(`git show a04c1ad:docs/reviews/local-checkpoint-compaction-2026-09-21.md`)
for results and measurement limits.

## Local reuse after publication

Successful ordinary dense uploads can retain their immutable sealed input as a
local logical equivalent of the published layer. Retention and mount pinning use
hardlinks, with no data copy. The next same-worker wake substitutes that local
file for the remote descriptor in the native source configuration. The journal
continues to store the remote published descriptors as checkpoint authority.

The default retained cache budget is 4 GiB, capped at ten percent of configured
hard storage capacity. Set `--published-local-cache-bytes` to change the budget
or zero to disable it. LRU eviction and physical free-space headroom protect disk
capacity; the reserve is the smaller of 1 GiB and five percent of the filesystem.
Active mount pins can outlive cache eviction and are released after safe device
release, including retired-device cleanup. Those active pins are not part of the
idle cache budget. Low space prevents new cache retention and hits; misses and
cache failures fall back to remote layers.

The index is process-local; restarting the storage service removes idle cache
links while journaled mount pins remain valid. Cache reuse is restricted to
confirmed dense exports and their exact source identity. Compacted uploads that
have no equivalent single local file continue to use the remote path. A local
compaction result subsequently uploaded as a dense layer is eligible.

Native `GetMetrics` exposes `published_local_cache_bytes`,
`published_local_cache_entries`, `published_local_cache_hits`,
`published_local_cache_misses`, and `published_local_cache_evictions`.
The existing `cache_bytes` metric counts unique allocated cached/pinned inodes,
including these files, rather than their sparse virtual sizes.

See the qualification report
(`git show a04c1ad:docs/reviews/published-local-cache-2026-09-21.md`).

## Write volume and trim

Before sealing a writable layer, the node runs `fstrim -m 1MiB` on its XFS
filesystem when the upper's allocated bytes exceed the filesystem's used bytes by
`--trim-before-seal-bytes` (default 256 MiB; zero disables it). It is best
effort, and there is no other runtime trim.

The 2026-09-22 production-worker qualification
(`git show a04c1ad:docs/benchmarks/write-volume-prod-2026-09-22/README.md`)
passed native tiered-compaction and XFS trim checks with the deployed backend.
Tiered selection reduced exported bytes by 87.48% on the synthetic overwrite
trace; trim removed 64.125 MiB of deleted payload, including with concurrent
foreground I/O. This does not establish full-load wake latency or authorize
turning on automatic trimming on other kernel/backend combinations.
