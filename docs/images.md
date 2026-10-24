# Images

How a sandbox gets its root filesystem: how the gateway resolves the image a
create names, where images are built, where their bytes live, and how a worker
mounts them. For the surrounding system see [architecture.md](architecture.md);
for the HTTP endpoints see [api.md](api.md).

## The model

A sandbox's root is a **signed environment**: a root manifest listing one or
more read-only filesystem **components**, each signed with the deployment's
Ed25519 producer key. A root names a base component, optional toolkit
components on top, the original image config and the process config
(Entrypoint, Cmd, Env, WorkingDir, User). Signing and conversion are
deterministic, so the same input always gives the same root digest and every
publish is idempotent.

Components come in two kinds, which differ only in where their bytes live:

| Kind | Bytes live in | Used for |
| --- | --- | --- |
| **RAFS** (Nydus RAFS v6) | The **chunk store**: 256 KiB chunks, deduplicated, in S3, read through the **store node** | Training images, prepared images, sandbox builds |
| **EROFS** | Blobs in the **managed registry** | Toolkits (`publish-environment`), images older EROFS builders published |

Workers trust only the signatures. S3, the store node, the chunk index and the
registry are untrusted: a worker verifies root and component signatures and
the digest of every metadata object before mounting, and every chunk after
decompression. A missing or corrupt chunk is an I/O error, never zeros.

```
create {image, toolkits?}
  │  gateway
  ├─ 1. image index name   → ensure: ready reference, or 503 image_building
  ├─ 2. managed id / ref   → digest-pinned reference
  ├─ 3. external ref       → import build, or 503 image_import_pending
  ├─ 4. root dispatch      → spec.environment_root = the image's signed root
  └─ 5. toolkits           → a composed root with toolkit components on top
  │  worker (environment-io backend)
  ├─ verify signatures, attach each component on an NBD device
  │    RAFS  → nydusd (or the Python RAFS device), reading the store node
  │    EROFS → Python NBD export, reading the registry
  ├─ kernel EROFS mount per component → bind (one) or read-only overlay (several)
  └─ per-sandbox writable upper → gVisor sandbox
```

Resolution is `_resolve_create_spec` in
[`control_plane.py`](../sandboxes/control_plane.py). The pinned root
keeps park, wake, migration and retries on the same root for the sandbox's
life; without `dispatch_roots`, the worker reads it from the manifest's
annotation.

**Released images.** Most chunk-store images have no OCI manifest any more
(prepared images, recipe and sandbox builds). They resolve from the gateway's
`image_roots` table (manifest digest → root, plus remembered tags), so nothing
on the create path needs the manifest.

## The image index

Training asks for a task's image by **name**, the same name it would send to
Prime (`aweaiteam/scaleswe:beetbox_beets_pr3661`), never a Dockerfile. The
gateway answers a training name only if it is in the image index, and every
index name starts from something already in the chunk store. The index also
tells the trainer which task ids it may sample.

Each name has an environment (`tmax`, `openswe`, `swe-smith`, ...), the task
ids that use it, its source dataset, and a kind:

- **prepared**: the whole image is already in the chunk store; ready from
  registration, never built.
- **recipe**: a build context and Dockerfile on top of a prepared base; built
  on first use, or ahead of time with ensure.

Registration refuses (`image_not_prepared`) an index name that does not start
from a prepared image. Names are `ready`, `not_built`, `building`, `retrying`
(all sampled) or `failed` (not sampled; e.g. a yanked package). The gateway
exports, per environment, the non-failed task ids as the taskset's
`task_ids_file`; re-export after builds fail for good.
[`scripts/images/build_image_index.py`](../scripts/images/build_image_index.py) fills the
index from a training inventory (`export`, then `register`; both rerunnable).

**Recipes.** Identical build inputs share one image id (`recipe-<sha40>`), so
a recipe builds once whatever names carry it; contexts are kept forever.
`POST /v1/images/ensure {"names": [...]}` returns each name's state and
submits missing builds; it is idempotent and cheap, and callers poll it. A
trainer ensures the next step's tasks so builds overlap the current step;
without it training is still correct, just slower on first use. Build state is
the gateway's, because builders forget builds when they scale down: lost
builds are resubmitted and failures retry a few times. A built recipe image is
released (OCI manifest deleted), since the recipe can always rebuild it.

## Prepared images

A **prepared image** is a validated chunk-store image that builds start from
(catalog: [`prepared_images.py`](../sandboxes/prepared_images.py)):

- **Sources**: faithful copies of upstream images, optionally with an exact
  enrichment recipe (SWE-smith's `swesmith-v1` bakes in task branches and
  ripgrep). A source can double as an import alias, so no import build runs.
- **Foundations**: an expensive, task-independent Dockerfile prefix built once
  and shared (the TMax base install, the OpenSWE Python/Conda prefix), keyed
  by pinned base digest and installer bytes.

For each build the gateway looks for an exact prepared match (never "a
similar image of the same project"), rewrites `FROM` to it and keeps the
remaining instructions byte-identical; the decision is frozen per request so
retries never change context. Unusual Dockerfiles skip matching and build
unchanged. Prepared images are made offline with the `scripts/images/plan_image_*`
and `scripts/images/prepare_image_*` tools, which validate the result and run a
smoke sandbox; `register_prepared_images.py` then adds their receipts to the
catalog.

## Where images are built

Ensure's submit asks the sandbox-build planner first; whatever it declines
goes to the builder pool.

**Sandbox builds** (`immutable_environments.sandbox_builds`). A task image is
usually a prepared foundation plus a few steps. A sandbox build runs only
those steps, in an ordinary sandbox on the workers, and stacks the result onto
the base's RAFS bootstrap; no OCI image and no builder VM are involved.

The gateway plans the remainder into one shell script plus the image config
Docker would record; only simple Dockerfiles qualify (`RUN`, `COPY`, `ENV`,
`WORKDIR`, `USER`, ...), anything else goes to the builders.
`serve-sandbox-builds` (gateway host) runs the script in a root sandbox
created through the gateway API, so placement and scale-up are the fleet's.
The sandbox's overlay upper becomes one layer, which is converted, merged onto
the base bootstrap and signed. `/tmp` and `/run` are not committed. Build
steps' apt and github.com git traffic go through the store node's package
cache and authenticated git proxy (GitHub refuses anonymous git from the
fleet). Only this service holds the S3 and signing keys, never the gateway's
API processes.

**Builder builds.** Builder VMs (`serve-builder-agent`) run Docker with
BuildKit; the gateway scales the pool up to `builder.max_nodes`. Each builder
admits a few builds and answers retryable 503s beyond that (no unbounded
queue). The output is converted into the chunk store, reusing layers already
converted (a foundation's). Optional: a shared
BuildKit cache (`builder.buildx_cache_ref`) and pull-through upstream mirrors
(`upstream_mirror`). Released images have no manifest for `FROM` to read, so
with `regenerate_bases` the gateway regenerates a verified one-layer OCI copy
from the chunk-store root and passes it to BuildKit as a build context.

**External imports.** A create naming an image outside the managed registry
maps it to `import-<sha40>` and submits a one-line `FROM <reference>` build.
Creates get a retryable 503 `image_import_pending` until it finishes. Each
external image is imported once per deployment.

## Toolkits

A **toolkit** is a versioned read-only tree under `/opt/ucloud/toolkits/<name>/`
that a sandbox gets without installing anything (uv and a Python, a harness's
prebuilt environment, agent harnesses' Node assets). It replaces per-sandbox
installs from PyPI or npm.

- A toolkit is a signed environment with one EROFS component, registered on
  the gateway as `name:tag`. Build scripts live in
  [`runtime/toolkits/`](../runtime/toolkits/).
- `SandboxSpec.toolkits` takes up to 4 refs (`name:tag` or
  `name@sha256:<root>`). The gateway pins tags to digests before placement,
  so a retried create keeps its toolkits even after a tag moves.
- The gateway composes a new signed root (image components, then toolkits on
  top), remembers it per (image root, toolkit set), and dispatches it. On the
  node it is just another overlay lower; nodes need no toolkit code.
- A toolkit never edits `PATH` or anything in the sandbox. The client points
  its own processes at it, so task commands and tests see the image unchanged.

## Storage

### The chunk store and the store node

The chunk store keeps every image's file data once, as 256 KiB chunks keyed by
the sha256 of their uncompressed bytes, packed into ≤64 MiB packs in S3.
Per component it also stores a RAFS **bootstrap** (filesystem metadata) and a
**chunk map** (device offset → chunk id), both signed, plus an unsigned
**locator** (chunk → pack range), which is only a hint. Conversion
([`chunk_convert.py`](../sandboxes/chunk_convert.py)) is shared by
every writer: run `nydus-image` per layer (reusing layers already converted),
pack only new chunks, upload, merge, sign, register, and publish the root
manifest last. A crash leaves nothing visible; a rerun yields the same root.

The **chunk index** (`serve-chunk-index`) is a SQLite index of chunks, packs,
layers and roots, rebuildable from S3; it makes concurrent writers pack each
chunk once. Writers hold its write token and the S3 key; workers only the read
token. Nothing is deleted from the chunk store yet (no GC; see
[roadmap.md](roadmap.md)).

The **store node** (`serve-chunk-store`, a dedicated private VM outside
placement) is the only way workers read the chunk store, because S3's tail
latency is seconds and workers reach it only through NAT. It serves range
reads with the read token, fills misses from S3 in aligned extents (shared
fills, hedged stalls), and assembles the blobs nydusd reads. It is an LRU
cache (`cache_bytes`; builder reads never evict training's working set) or,
with `replica: true`, a full mirror. Losing it loses no data, but cold
attaches of chunk-store images fail with EIO until it is back.

### The managed registry

A Docker Distribution instance on the gateway host, private network only.
Clients never see it: they use image ids. It holds signed root and component
manifests (for RAFS and EROFS alike), EROFS blobs (toolkits, `erofs` builds),
OCI images that keep their manifests (builder outputs, imports, user builds),
regenerated bases and the BuildKit cache. An hourly
`registry-prune` deletes old unused tags and unreferenced roots; leases and
durable references protect anything in use. Freeing blobs needs a sweep that
stops the registry, so it runs only in a maintenance window (see
[operations.md](operations.md)).

## On the worker

Each worker runs `ucloud-environment-io.service` (`serve-environment-io`), a
backend separate from the node agent so agent restarts never detach mounted
filesystems. It owns the NBD devices and EROFS mounts behind a private Unix
socket. The node agent resolves a root (dispatched, else annotation), asks the
backend to attach every component, and composes them.

- **RAFS:** a pinned stock `nydusd` per image (with `chunk_store.nydusd`,
  else the backend's own RAFS device) reads the store node and verifies every
  chunk. A stall delays a read; a dead store node reads as EIO.
- **EROFS:** the backend's Python NBD export fetches chunks from the registry
  by range, verifies them and caches them on the node.
- One component is bind-mounted; several (layer groups, toolkits) form a
  read-only overlay, shared by every sandbox of that image. Each sandbox gets
  its own writable upper plus tmpfs at `/tmp` and `/run`.
- A deleted sandbox's image stays mounted for the next one; idle images are
  collected LRU only when the node is over its device or cache budget.
  Prefetch (metadata hints, startup traces) only orders fetches.

## Configuration

All settings are in `deployment.json` under `immutable_environments` (plus
`builder`, `upstream_mirror` and the `registry_*` keys). Run
`uv run sandboxes sample-config` for a full example, and see
[`config.py`](../sandboxes/config.py) for every key, default and
validation rule. The keys that select behaviour:

| Key | Effect |
| --- | --- |
| `immutable_environments.worker_enabled` / `builder_enabled` | Workers run the environment backend; builders convert builds into the chunk store |
| `immutable_environments.dispatch_roots` | Gateway pins the signed root into every create spec |
| `immutable_environments.chunk_store` | Enables the chunk store (S3 location, index, tokens); `store_node` and `nydusd` inside it |
| `immutable_environments.sandbox_builds.enabled` | Build eligible recipes in sandboxes; needs `dispatch_roots` and a store node |
| `immutable_environments.regenerate_bases` | Regenerate OCI copies of released bases for builder builds |
| `builder.max_nodes` | Builder VMs at most |

## Operator commands

`image-index` reads through the gateway API with the SDK key
(`UCLOUD_SANDBOX_URL`, `UCLOUD_SANDBOX_API_TOKEN`); the rest run on the
relevant host with `--config`.

```bash
sandboxes image-index summary | show <name> | list --environment E --state failed
sandboxes image-index export-task-ids <dir>        # one task_ids_file per environment
sandboxes publish-environment --environment-allow-path opt/ucloud/toolkits/<name> ...
sandboxes toolkit-register --name N --tag T --image-ref R ; toolkit-list
sandboxes warm-chunk-store --component <digest> --wait   # fill the store node before a burst
sandboxes convert-environment | unpack-environment ...  # registry → chunk store, and back (rollback)
sandboxes chunk-migrate inventory|convert|record|switch|revert|status  # dry run until --execute
sandboxes registry-prune [--execute]
sandboxes provision-environment-key                # create or recover the producer key pair
```
