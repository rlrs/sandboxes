# Images

This document describes how a sandbox gets its root filesystem: how the gateway
resolves the image a create names, how images are prepared and built, where
their bytes are stored, and how a worker turns them into a mounted, read-only
root with a writable layer on top.

## Overview

A sandbox's root filesystem is a **signed environment**: a root manifest that
lists one or more components, each a read-only filesystem image signed with
the deployment's environment producer key. The component kinds differ in where
their bytes live:

- **RAFS components** (`ucloud-environment-rafs-v1`) are Nydus RAFS v6 images
  whose 256 KiB chunks live, deduplicated, in the **chunk store**: S3 object
  storage, read by workers through a **store node** on the private network.
  Training images are RAFS.
- **EROFS components** (`ucloud-environment-erofs-v2`, and older whole-image
  v1 components) are EROFS images stored as blobs in the **managed registry**.
  Toolkits and images built with `builder_format: "erofs"` are EROFS.

From a create request to a mounted root:

```
create {image: name | managed id | managed ref | external ref, toolkits?}
  │
  ▼  gateway (_resolve_create_spec)
  1. image index name   → ensure: ready reference, or 503 image_building
  2. managed reference  → digest-pinned reference (image records, image_roots)
  3. external reference → import build, or 503 image_import_pending
  4. dispatch_roots     → spec.environment_root = the image's signed root
  5. toolkits           → a composed root with toolkit components on top
  │
  ▼  worker
  environment-io backend: verify signatures, attach each component
    RAFS  → nydusd (or the Python RAFS device) on an NBD device, reading the store node
    EROFS → Python NBD export, reading verified chunks from the registry
  kernel EROFS mount per device → bind (one component) or read-only overlay (several)
  → per-sandbox writable upper → gVisor sandbox
```

Images get into the chunk store in three ways:

1. **Sandbox builds:** a recipe whose base is a prepared chunk-store image runs
   its remaining Dockerfile steps in an ordinary sandbox on the workers; the
   result is stacked onto the base's RAFS bootstrap.
2. **Builder builds:** Docker/BuildKit on builder VMs, ending in the chunk
   store (`builder_format: "rafs"`) or as registry EROFS components
   (`"erofs"`, the default).
3. **Conversion:** `convert-environment` and `chunk-migrate` convert an
   existing registry image.

### Where image state lives

| Store | Where | Holds |
| --- | --- | --- |
| `images.sqlite` | gateway state directory | Managed image ids and their pinned manifest digests |
| `image-recipes.sqlite3` | beside `images.sqlite` | The image index: names, recipes, prepared references, build state |
| `<images>-recipe-contexts/` | beside `images.sqlite` | Recipe build contexts, kept without expiry |
| `image-roots.sqlite3` | beside `images.sqlite` | Image → dispatched root (`image_roots`), remembered tags, regeneration receipts, journal |
| `prepared-images.sqlite3` | beside `images.sqlite` | Prepared sources and foundations; frozen build decisions |
| `toolkits.sqlite3` | beside `images.sqlite` | Toolkit `name:tag` → root, and every composition |
| `registry-usage.sqlite` | gateway data root | Registry references, leases and last-use times |
| `build-history.sqlite` | beside `metrics.sqlite` | Build timing summaries |
| Chunk index (SQLite) | store node (or gateway) | Chunk → pack, layer conversions, registered roots |
| S3 bucket, under `chunk_store.prefix` | object storage | Packs, bootstraps, chunk maps, locators, blob tails and layouts |
| Store node cache | store node disk | A read-through cache or full replica of the S3 prefix |
| Managed registry | gateway host | Signed root and component manifests, EROFS blobs, OCI images still kept, build cache |

`image-roots.sqlite3`, `prepared-images.sqlite3`, `image-recipes.sqlite3` and
`toolkits.sqlite3` are created with `CREATE TABLE IF NOT EXISTS` and no strict
schema check, so an older release ignores them.

## Signed environments

An environment root is an OCI manifest in the environment repository
(`immutable_environments.repository`, default `environments`) with no layers.
It names a base component, an optional workspace component, ordered toolkit
components, the original image's config digest (`source_image`) and the
process configuration (Entrypoint, Cmd, Env, WorkingDir, User). Each component
is signed with Ed25519 under its own domain; a RAFS component signs its
bootstrap and chunk-map digests and sizes, the device size, the format
(`nydus-image v2.4.5`, 256 KiB chunks, sha256 digester, zstd) and the source
layers' diff IDs.

Signing is deterministic and conversion is reproducible, so the same input
always yields the same root digest. Content addressing makes every publish
idempotent: a crash leaves nothing visible, and a rerun produces the same root.

**Trust.** Gateways, builders and workers load the producer trust file
(`trusted_keys_file`: a JSON map of `sha256:<key hash>` to a base64 Ed25519
public key, owned and not writable by others). The private key
(`signing_key_file`) is held only by the gateway (which signs toolkit
compositions), builders and the sandbox-build service. Workers verify the root
and component signatures, the root's `source_image`, and the digest and size
of every bootstrap and chunk map **before any privileged ioctl**, and every
chunk after decompression. S3, the store node, the chunk index and locators
are untrusted. A missing or corrupt chunk is an I/O error, never zeros.
`provision-environment-key` creates or recovers the producer key pair; see
[security.md](security.md).

**Which root a sandbox gets.** An image's root is found in one of two ways:

- **Dispatched root.** With `immutable_environments.dispatch_roots`, the gateway
  sets `environment_root` in the create spec: the root recorded for the image
  in `image_roots`, else the root its manifest annotation names. The spec pins
  that root for the sandbox's life, so park, wake, migration and retries keep
  it. A spec with `environment_root` is placed only on workers whose heartbeat
  advertises `environment-root-dispatch-v1` and `environment-rafs-v1`;
  without one, the create answers retryable `no_ready_node`. The worker loads
  the dispatched root directly and, while the manifest still exists, requires
  its `source_image` to equal the manifest's config digest.
- **Annotation.** Without a dispatched root, the worker reads the root digest
  from the image manifest's `org.ucloud.immutable-environment.v1` annotation.

**`image_roots` rows** map a pinned (annotated) manifest digest to its new
root. They move `converted` → `switched` → `released`, or to `reverted`; every
change is journaled. `switched` and `released` rows dispatch their new root.
Images built in the chunk store are `released` from the start.

**Released images** have no OCI manifest. Their tags are remembered in
`image_tags` (except `ucloud-digest-*` tags) before the manifest is deleted.
Every reader asks the registry first and, on a 404, answers from the
`released` row (a tag through its remembered digest): create resolution
(`ImageResolution.resolve_and_protect_manifest`, which covers digest and tag
resolution, `enrich_records` and `record_missing_manifest`), the dependency
resolver, and the hourly prune's stale-build-record check. The gateway's
per-node pull carries the dispatched root, so a worker never needs the
manifest. Pressure eviction skips dispatched images. A builder build that
names a released image gets a regenerated copy (see
[Base regeneration](#base-regeneration)), and a commit on a released parent
is refused (`commit_parent_released`).

To roll an image back after release, regenerate its OCI image with
`unpack-environment` (the tree is exact; the digests are new), rebuild an EROFS
root from it with a builder, and repoint the image's `image_roots` row. Before
release, `chunk-migrate revert` restores the old roots from the journal in
seconds.

## Resolving an image at create

The gateway resolves `spec.image` in this order (`_resolve_create_spec` in
[`control_plane.py`](../ucloud_sandboxes/control_plane.py)):

1. **Image index name.** If the name is registered in the image index, the
   gateway runs ensure for it. A ready name is replaced by its pinned
   reference. A name still building answers 503 `image_building`
   (`Retry-After: 15`, retryable), and the call itself starts the build. A
   name that failed for good answers 409 `image_build_failed`.
2. **Managed image id or reference.** `ImageResolution.resolve` rewrites the
   reference to `repository@sha256:<annotated manifest digest>` and protects
   that digest with a `ucloud-digest-sha256-<hex>` tag. A released image
   resolves from `image_roots`. A managed image evicted for registry space
   answers 400 `image_evicted` with `rebuild_required: true`.
3. **External reference** (for example a Docker Hub image): imported, see
   [External image import](#external-image-import).
4. **Root dispatch**, when `dispatch_roots` is on.
5. **Toolkits**, when the spec names any: see [Toolkits](#toolkits).

A group create resolves one spec per group. `POST /v1/images/pull` warms an
image onto nodes ahead of creates; see [api-reference.md](api-reference.md).

## The image index

Training asks for each task's image by **name**, the same name it would send
to Prime (`prime/primeintellect/tmax:task_000123_abcd`,
`aweaiteam/scaleswe:beetbox_beets_pr3661`), and never sends a Dockerfile. The
gateway answers a training name only if it is in the image index, and every
name in the index is backed by something already in the chunk store. The index
also tells the trainer which tasks it may sample.

The index is the recipe store (`gateway/image_recipes.py`). Each name has:

- an **environment**: `tmax`, `terminal-lego`, `openswe`, `scaleswe`,
  `r2e-gym`, `swe-lego`, `swe-rebench-v2`, `multiswe` or `swe-smith`;
- its **tasks**: the dataset task ids that use the image (one SWE-smith image
  serves many tasks; elsewhere it is usually one);
- its **source**: dataset, revision, inventory and environments revision;
- its **kind**:
  - `prepared`: the whole image is in the chunk store
    (`prepared_reference`, `<repository>@sha256:<digest>` without a host). It is
    ready from registration and never built.
  - a **recipe**: a build context and Dockerfile that build on a prepared base.
    It builds on first use, or ahead with ensure.

Registration refuses (`image_not_prepared`) a prepared name whose image is not
in the chunk store, and an index name (one with an environment) whose recipe
builds on no prepared base. So "in the index" means "starts from something
prepared". A recipe registered without an environment is an ordinary
build-on-demand name and needs no prepared base.

### States

| State | Meaning | Sampled |
| --- | --- | --- |
| `ready` | Prepared, or built | yes |
| `not_built` | A recipe not built yet; the first create waits for its build | yes |
| `building` | Its build is submitted or running | yes |
| `retrying` | Its build failed and will be retried | yes |
| `failed` | Failed for good (recipe rot such as yanked packages), or a prepared image the chunk store no longer holds | no |

### Task-id export

The gateway exports, per environment, the task ids whose names are not
`failed`, as the JSON array a taskset reads as its `task_ids_file`. The ids are
what each taskset filters on:

| Environment | Task id |
| --- | --- |
| `tmax`, `terminal-lego` | The task's directory name |
| `r2e-gym` | `commit_hash` |
| `swe-smith` | `<language>:<instance_id>` |
| The others | `instance_id` |

Re-export after builds fail for good, so the trainer stops sampling them. A
failed name stays visible in `image-index list --state failed` with its error.

### Reading and filling the index

`ucloud-sandboxes image-index` reads the index through the gateway API with the
SDK key (`--gateway-url` and `--api-token-file`, or `UCLOUD_SANDBOX_URL` and
`UCLOUD_SANDBOX_API_TOKEN`):

```bash
ucloud-sandboxes image-index summary                 # names and tasks per environment, by state
ucloud-sandboxes image-index show 'aweaiteam/scaleswe:beetbox_beets_pr3661'
ucloud-sandboxes image-index list --environment openswe --state failed
ucloud-sandboxes image-index task-ids tmax --out tmax.task-ids.json
ucloud-sandboxes image-index export-task-ids allowlists/   # <environment>.task-ids.json for each, plus summary.json
```

The same views are `GET /v1/image-index`, `/v1/image-index/names?environment=&state=&after=&limit=`,
`/v1/image-index/name?name=` and `/v1/image-index/task-ids?environment=`.

[`scripts/build_image_index.py`](../scripts/build_image_index.py) fills the
index from a training inventory in two steps. `export`, where the datasets
are, writes one manifest row per name: `prepared` when nothing is left to
build, otherwise its recipe (checked against the inventory's recipe hash),
with the task ids read from the datasets at their pinned revisions.
`register`, against the gateway, uploads contexts and registers in batches;
refused names land in `<environment>/refused.jsonl`. Both steps are safe to
rerun. [`scripts/import_image_recipes.py`](../scripts/import_image_recipes.py)
registers TMax and Terminal-Lego recipes from their exports.

## Recipes and ensure

**Registration.** `POST /v1/image-recipes` takes up to 1,000 rows:
`{name, context_archive_digest, context_archive_size, dockerfile?, build_args?, retention?}`
for a recipe, or `{name, prepared_reference, retention?}` for a prepared
image, each with optional `environment`, `tasks` and `source`. Contexts are
uploaded first with `PUT /v1/image-contexts/<digest>`. With `"partial": true`
the acceptable rows register and refused ones come back with reasons;
otherwise any refusal refuses the request.

- **One image per recipe.** Identical build inputs (context digest,
  Dockerfile, build arguments) share one gateway-managed image id,
  `recipe-<sha40>`, so a recipe builds once whatever names it carries.
  Re-registering an unchanged recipe is a no-op; a changed recipe moves its
  name to a new image.
- **Contexts are kept.** Uploaded contexts age out of the gateway's upload
  store, so registration copies each into the recipes' own context store,
  which never expires, and puts it back before a build.
- **Retention.** `pinned` images keep a durable reference; `cached` (the
  default) ones are rebuilt on demand if they are ever lost.

**Ensure.** `POST /v1/images/ensure {"names": [...]}` (up to 1,000 names)
returns each name's state: `ready` with `reference`, `building` with
`build_id`, `queued` (no slot yet, or past this call's submission budget),
`failed` with `error` and `attempts`, or `unknown`. Missing images are
submitted through the gateway's ordinary build dispatch, at most 32 per call,
so the call is idempotent and cheap and callers poll it. A trainer calls it
for the next step's tasks so their builds overlap the current step; without
that call training is still correct, only slower on a task's first use.

**Build state is the gateway's**, because builders forget builds when they
scale down. Each recipe image has a row (`absent`, `submitting`, `building`,
`ready`, `failed`):

- A submission is claimed atomically, so no two gateway processes submit one
  image.
- A finished build's image record is adopted, so it resolves after its
  builder is gone.
- A build no builder knows about is resubmitted after 180 s.
- A failed build is retried after 10 minutes, 3 attempts in all. A failure of
  the build machinery itself (sandbox-build errors from the chunk store, S3 or
  the registry) retries after 30 s, doubling up to 10 minutes, 6 attempts in
  all, so an object-storage outage neither stalls a first-use build for 10
  minutes nor uses up the recipe's attempts.

**OCI release.** When ensure first finds a recipe image ready with a
chunk-store root, it records an `image_roots` row (wave `recipe`, state
`released`) and deletes the image's OCI manifest, at most 64 per call. A
lease or route still reading the manifest defers this to a later call. No
regeneration receipt is needed: a recipe can always rebuild its image. An
image built as EROFS keeps its OCI copy.

## Prepared images and foundations

A **prepared image** is a validated artifact that later builds can start
from. The gateway's prepared catalog (`prepared-images.sqlite3`,
[`prepared_images.py`](../ucloud_sandboxes/prepared_images.py)) holds two
kinds:

- **Sources:** faithful copies of upstream images (Docker Hub, GHCR, Microsoft
  registries), resolved to linux/amd64 digests, plus exact enrichment recipes
  such as SWE-smith's `swesmith-v1` (fetch all task branches, install
  ripgrep). A source registered as an import alias resolves creates that name
  the upstream reference, with no import build.
- **Foundations:** an expensive, task-independent Dockerfile prefix built once
  and shared: the TMax `base_install.sh` stage or literal leading apt/pip
  commands of inline installers, the OpenSWE Python/Conda prefix, and leading
  Terminal-Lego dependency instructions. A foundation's identity covers the
  pinned base digest, platform and installer bytes, never local paths or task
  files.

**Build-time matching.** For every build of an uploaded context, the gateway
looks for a prepared base (`PreparedImageCatalog.choose`): a literal source
reference, an exact dependency-prefix identity including installer bytes, or
the exact SWE-smith enrichment recipe. A different task image of the same
project never counts as equivalent. Terminal-Lego matching takes the longest
prepared prefix at complete instruction boundaries, bounded to 256
instructions. A literal external `COPY --from=image:tag` in a single unnamed
stage is accepted. Matching is skipped for build arguments, alternate
Dockerfiles, a nonempty `.dockerignore`, custom frontends, multiple stages,
broad context copies, bind mounts, and contexts over 8 MiB of file data or
1,024 entries; such builds run unchanged. The raw build API accepts
`prepared_cache: "off"` to bypass matching.

On a match the gateway leases the prepared digest and derives a deterministic
context whose `FROM` names it; the remaining instructions keep their bytes.
Accepted submissions carry `prepared` metadata (`kind`, `reference`, and the
foundation `key`). The request fingerprint freezes the decision, hit or miss,
across retries and gateway restarts (`prepared_decisions`), so a retry never
silently changes its context. Prepared snapshots keep the package versions
resolved when they were prepared, like a build cache.

**Preparation tools** run offline as the gateway service account:
[`plan_image_foundations.py`](../scripts/plan_image_foundations.py) and
[`prepare_image_foundations.py`](../scripts/prepare_image_foundations.py) for
foundations, [`plan_image_pool.py`](../scripts/plan_image_pool.py) and
[`prepare_image_pool.py`](../scripts/prepare_image_pool.py) for sources, and
[`register_prepared_images.py`](../scripts/register_prepared_images.py) to
register existing receipts. Each validates the signed artifact, runs a smoke
sandbox, and protects the result with a durable owner
(`image-foundation:<key>`, `image-pool:<key>`). Only validated results enter a
catalog. Foundations with unpinned upstream packages freeze what they
resolved; a refresh needs new pinned inputs and so a new identity.

Prepared images live in the chunk store. Their OCI manifests have been
released, and a builder build that names one gets a regenerated OCI copy (see
[Base regeneration](#base-regeneration)).

## Builds

A recipe build takes one of two paths. Ensure's submit asks the sandbox-build
planner first; whatever it declines goes to the builder pool.

### Sandbox builds

A task image is usually a prepared foundation plus a few steps: copy the task
files, install a few packages, bootstrap a verifier. A sandbox build runs only
those steps, in a sandbox on the workers, and stacks the result onto the base's
RAFS bootstrap. No OCI image is read or written and no builder VM is needed.

1. **Decide** (`gateway/sandbox_builds.py`). The prepared catalog must split
   the recipe into a base (a foundation or source) and a remainder; the base
   must dispatch one whole-image chunk-store root; and the remainder must plan.
   Build arguments, another Dockerfile path or a `.dockerignore` send the build
   to the builders.
2. **Plan** (`sandbox_build.plan_build`, pure). The remaining Dockerfile becomes
   one POSIX shell script plus the image config Docker would record.
3. **Run.** `serve-sandbox-builds` (`ucloud-sandbox-builds.service` on the
   gateway host) creates an ordinary managed sandbox from the base root through
   the gateway API (root, all capabilities, as a Docker build step has;
   `cpus`, `memory_mb` and `disk_mb` from config), so placement, capacity and
   scale-up are the fleet's. The script and context arrive as one archive
   under `/var/tmp/.ucloud-build` and run as one managed job.
4. **Export.** The worker's admin-only `commit-export` turns the sandbox's
   overlay upper into a sorted OCI layer tar; the sandbox is deleted.
5. **Stack** (`RafsConverter.extend`). Only that layer is converted;
   `nydus-image merge --parent-bootstrap` stacks it on the base's bootstrap,
   and the result is signed as a whole-image RAFS root.

The gateway and the service communicate through a spool
(`<state>/sandbox-builds/jobs/<image>.json` and `results/<image>.json`). The
service holds the chunk store's S3 key, the index write token and the signing
key; the gateway's API processes never hold the S3 key. `slots` builds run at
once and `stack_slots` of them stack at once.

**What a sandbox build runs.** Recipes reach the planner only after the
catalog's `safe_rewrite`: one `FROM`, and no `ARG`, `ADD`, heredocs, `RUN --`
flags or COPY globs and flags other than `COPY --from=<literal image>`.

| Instruction | Sandbox build |
| --- | --- |
| `RUN` (shell or JSON form) | `env -i` with the image's Env at that step, plus `HOME` (if unset), `TMPDIR` and `APT_CONFIG`, in its WORKDIR, as its USER through `setpriv` |
| `COPY src... dest` | Docker's rules: directory contents, `dest/` semantics, parents made, owner 0:0, modes, mtimes and symlinks kept |
| `COPY --from=<public image>` | The gateway host pulls the named paths (linux/amd64, anonymous, cached by digest) from `docker.io`, `ghcr.io`, `quay.io`, `gcr.io`, `registry.k8s.io`, `public.ecr.aws` or `mcr.microsoft.com` |
| `ENV`, `WORKDIR`, `USER`, `SHELL`, `CMD`, `ENTRYPOINT` | Recorded as Docker does, with `$VAR`, `${VAR}`, `${VAR:-x}` and `${VAR:+x}` substitution; an `ENTRYPOINT` resets an inherited `CMD` |
| `LABEL`, `EXPOSE`, `VOLUME`, `STOPSIGNAL`, `HEALTHCHECK`, `MAINTAINER` | Ignored: a root's config has no place for them and sandboxes do not use them |
| Anything else | The builders |

Where it deliberately differs from Docker:

- `/tmp` and `/run` are not committed; sandboxes mount tmpfs there.
- Steps get `TMPDIR=/var/tmp/.ucloud-build-tmp`, removed before the commit.
  A recipe that writes to `/tmp` by name gets a tmpfs of half the build's
  memory there.
- `HOSTNAME` is not set in steps.
- Package-manager caches (pip, uv, npm, apt archives) are kept, as in a Docker
  image.

**Package and git caches.** Steps' apt requests to allowlisted archive hosts
go through the store node's package cache
(`chunk_store.store_node.package_cache`), which can serve a host from a faster
mirror of the same tree, keeps package files indefinitely (LRU past
`max_bytes`) and index files for `index_seconds`. With `github_token_file`,
git fetches from github.com are redirected (`url.<proxy>/github/.insteadOf`)
through the cache's authenticated, fetch-only git proxy, because GitHub
refuses anonymous git from the fleet's addresses. Other traffic goes direct.

**Images born in the chunk store.** A sandbox-built image never had an OCI
manifest. Its identity is the digest of the manifest it would have had (its
config, no layers, the root as annotation). Ensure adopts it as an
`image_roots` row, `released` from the start (wave `sandbox-build`), and a
gateway image record with source `build:sandbox`. Resolution, dispatch,
pinning and toolkits treat it like any other released image.

A failed step fails the attempt with the step number, exit code and the end
of its output; ensure's retry rules apply.

### Builder builds

Builder VMs (`serve-builder-agent`, the `builder` pool) run Docker with
BuildKit. The gateway routes `POST /v1/images/build` to a ready builder,
scaling the pool up to `builder.max_nodes`; idle builders scale down after
`builder.scale_down_idle_seconds`. A build ends in its publisher:

- **`builder_format: "erofs"`** (default). The builder publishes the image as
  signed EROFS components in the registry and annotates the pushed manifest
  with the root.
  - **Layer groups.** The image's original layer diffs are grouped bottom-up
    around 64 MiB of compressed input, at most 24 groups. Each group is one
    component whose identity binds its ordered diff IDs, parent ChainID,
    layout, mkfs version, compression and exclusions, so related images
    share their lower groups (indexed by `layer-*` tags). Unsplittable inputs
    fall back to one whole-image component.
  - **Format.** `mkfs.erofs` with lz4 compression. Layout 1 sets every mtime
    to 0, which makes Python recompile every `.pyc`. Layout 2
    (`preserve_mtimes`, needs erofs-utils 1.9) keeps file mtimes and writes
    all metadata into one zone (`--MZ`). The layout is part of the group key;
    workers read both.
  - **Allowlist.** Only `allow_paths` are published; `*` publishes every
    top-level path except `dev`, `proc`, `sys` and `run`.
  - **Metadata hints.** The builder walks the EROFS image and signs a hint of
    which chunks hold metadata, carried as the component manifest's
    `org.ucloud.environment.metadata.v1` annotation (see [Prefetch](#prefetch)).
  - **Partial hits.** If some groups already exist and the missing layers'
    diffs are self-contained (up to 128 MiB compressed, 1 GiB unpacked), the
    builder extracts only those, in a subprocess, instead of materializing
    the image through Docker.
- **`builder_format: "rafs"`**. The last step converts the pushed image into
  the chunk store (`chunk_convert.rafs_build_publisher`) and moves the tag to
  a copy annotated with the RAFS root. Layers another build already converted
  (a foundation's) are reused through the index's layer claims, so a task
  image costs its own layers. There is no full-tree check: these images can
  be rebuilt from their recipes. RAFS builders get the chunk-store block, the
  index write token and the S3 key (`/etc/ucloud-sandboxes/chunk-store.env`,
  root-only), never the read token, and install the `nydus-image` pinned by
  `chunk_store.nydus_image_sha256`.

**Capacity.** Each builder admits four nonterminal builds (context
preparation, Docker build and push), and optionally one or two more slots for
filesystem publication and cleanup (`builder.max_finishing_builds`, default
0). A build keeps its build slot until a finishing slot is free, so slow
publication applies backpressure. The builder publishes its admission ceiling
in the heartbeat label `ucloud.image-build-admission-capacity`; requests
beyond it get retryable 503s, which consume the client's submission budget.
There is no unbounded build queue.

**Deadlines.** `builder.build_execution_timeout_seconds` (default 1,800)
bounds execution from when the build worker starts: Docker build, push and
filesystem publication share it. On expiry the builder terminates its own
Docker/Buildx process group (never the shared BuildKit daemon), then fails the
build. An accepted build has a stable id: a client that stops waiting does not
cancel it, and a status timeout is a retryable 503/504, not a missing build.
Builds are not replayed after their builder is lost; for recipes, ensure
resubmits them.

**Shared BuildKit cache.** With `builder.buildx_cache_ref` (a repository in
this deployment's registry named `ucloud-build-cache` or `ucloud-build-cache-*`),
every builder imports and exports a registry-backed BuildKit cache
(`mode=min`) through the `ucloud-shared-cache` docker-container driver. Each
build exports a unique tag; imports prefer an exact context match, then the
same Dockerfile and recent recipes, up to eight. The hourly prune keeps at
most `buildx_cache_max_entries` tags, `buildx_cache_max_bytes` of unique blobs
and `buildx_cache_max_age_seconds` of age, preferring distinct contexts.
Cache lookup and export failures never fail a build. Before a push, the
builder can mount blobs from a matching cache entry into the output
repository, so a fresh BuildKit store does not upload existing layers again.

**Upstream mirror.** With `upstream_mirror`, each public upstream registry
gets a pull-through Distribution instance on the gateway, so builders fetch
each upstream blob once, with an authenticated Docker Hub account. Builders
and nodes try the mirror first and fall back to the upstream; pulls are
digest-verified either way. An hourly trim keeps the caches under
`max_bytes`. Upstreams other than `docker.io` need `builder.buildx_cache_ref`.

**Build history.** Terminal build results observed by the gateway are kept in
`build-history.sqlite` (at most 10,000 summaries, 32 MiB, 30 days): ids,
status, timestamps and timings (`preparation_ms`, `queue_wait_ms`,
`end_to_end_ms`, per-phase timings), never logs, commands or contexts.

### Base regeneration

A builder build reads its `FROM` base from the registry, but prepared and
migrated images have no OCI manifest any more. With
`immutable_environments.regenerate_bases`, the gateway regenerates a copy of
a released image a build names (`gateway/base_regeneration.py`):

- The build gets `--build-context <reference>=docker-image://<copy>`, so the
  Dockerfile, context, catalog pins and cache affinity stay byte-identical;
  only BuildKit's source changes.
- The copy is one layer, unpacked from the chunk-store root
  (`nydus-image unpack`), under the image's original config, so `ONBUILD`,
  `SHELL`, `ENV`, labels and volumes are inherited as before. It must
  reproduce the diff ID recorded by `chunk-migrate verify-regeneration`
  before the image's OCI was released; otherwise it is refused.
- A build whose copy is missing answers 503 `base_regenerating`
  (`Retry-After: 30`) while the gateway regenerates it (two at a time per
  host, with 16 GiB of scratch reserve). A failure answers 409 `base_released`
  for 10 minutes, then retries.
- Copies live in `ucloud-regenerated` and are pruned 30 days after their last
  build; each build leases its copy.

A commit whose parent image's OCI was released is refused
(`commit_parent_released`).

## External image import

A create that names an image outside the managed registry is imported
transparently ([`image_import.py`](../ucloud_sandboxes/image_import.py)):

1. The reference maps to the managed id `import-<sha256(reference)[:40]>`.
2. The gateway submits a one-line context (`FROM <reference>`) as an ordinary
   managed build, which pulls the image (through the upstream mirror, if
   configured), pushes it and publishes its environment before recording the
   image. Once the id resolves, the image has its environment.
3. A create while the import runs gets 503 `image_import_pending`
   (`Retry-After: 5`, retryable); the SDK retries until its deadline.
   `prepare_capacity` with an external image starts the import early.
4. If the latest import failed, creates get 400 `image_import_failed` with the
   build error. Imports are resubmitted at most every 30 s per gateway
   process, so a transient failure recovers on a later create.

Each external image is imported once per deployment. An import alias from the
prepared catalog resolves the reference with no build at all.

## Toolkits

A **toolkit** is a versioned, read-only tree under
`/opt/ucloud/toolkits/<name>/` that a sandbox gets without installing
anything: for example uv, a standalone Python and a harness's prebuilt
environment, or the agent harnesses' Node assets. It replaces per-sandbox
installs from PyPI or npm; on a 512-rollout dry run, harness setup went from a
71.8 s median to 4.2 s.

- **Format.** A toolkit is a signed environment whose root has exactly one
  whole-image EROFS component. It lives in the registry and is served by the
  Python export; a composition is mounted once per node and shared, so its
  pages cross the export once.
- **Build and register.** Build an image whose files are all under the
  toolkit's path, publish it with
  `publish-environment --environment-allow-path opt/ucloud/toolkits/<name>`,
  then register it on the gateway host with
  `ucloud-sandboxes toolkit-register --config ... --name <name> --tag <tag> --image-ref <ref>`.
  `toolkit-list` lists registrations. Build scripts live in
  [`runtime/toolkits/`](../runtime/toolkits/) (`vf-harness`, `vf-agents`).
- **Request.** `SandboxSpec.toolkits` holds at most 4 refs, `name:tag` or
  `name@sha256:<root>`. The gateway pins tags to root digests before
  placement; the stored spec carries digests only. The field is omitted when
  empty, so existing spec fingerprints are unchanged. A retried create keeps
  the toolkits it first pinned, even after a tag moves.
- **Composition** (`gateway/toolkits.py`). The gateway appends the toolkit
  components to the image root's `toolkits` list, keeping its base,
  `source_image` and config, signs the result and dispatches it as the
  sandbox's `environment_root`. A composition is content-addressed,
  remembered per (image root, toolkit set), and built once even under
  concurrent creates. The image and its toolkits may have at most 33
  components. Toolkits on committed images are not supported yet.
- **On the node,** a RAFS image plus a toolkit becomes a read-only overlay of
  two lowers, the toolkit on top. Nodes need no toolkit-specific code.
- **No environment changes.** A toolkit never edits `PATH` or anything else in
  the sandbox. The client points its own processes at it (verifiers-ucloud
  sets `UV_*` variables and `PATH` for the harness's processes only), so task
  commands and tests see the image unchanged.

A toolkit's commit chain keeps it: a committed image's parent is the
composed root.

## The chunk store

The chunk store keeps every image's file data once, as 256 KiB chunks keyed by
the sha256 of their uncompressed bytes, in S3. The S3 code is
provider-agnostic; production uses Hetzner Object Storage. About 1 in 100
requests to it stalls for 20 s or more, so clients bound every read with a
5 s stall timeout and retry at once instead of waiting.

### Data model

| Object | Content | Signed | S3 key under the prefix |
| --- | --- | --- | --- |
| Chunk | At most 256 KiB of one file's data; id = sha256 of the uncompressed bytes. Stored zstd or raw | id checked on every read | inside a pack |
| Pack | At most 64 MiB: a 16-byte header (`UCPK`), chunk payloads in image address order, a footer sorted by id, a trailer. Named by its sha256, so uploads are idempotent | no | `packs/<hh>/<sha256>.pack` |
| Bootstrap | RAFS v6 metadata (inodes, directories, device table, chunk table), zstd, at most 128 MiB | digest and size | `meta/<sha256>.boot.zst` |
| Chunk map | `ucloud-chunk-map-v1`: blob regions, then `(device offset, length, chunk id)` entries sorted by offset | digest and size | `meta/<sha256>.map` |
| Locator | Per component and epoch: each map entry's pack and range. A hint only: a wrong locator causes a verified miss | no | `meta/<component>.<epoch>.loc`, also kept in the index |
| Blob tail and layout | Per nydusd blob: its chunk table and where those chunks are stored | no | `meta/<blob>.tail`, `meta/<blob>.layout` |

Chunk encoding: the packer keeps the zstd bytes nydus-image emitted and never
recompresses; it stores a chunk raw when zstd saves under 3% or the chunk is
under 4 KiB. The id covers the uncompressed bytes, so two writers' encodings
of a chunk are interchangeable; the first committed copy wins, and readers
verify after decompression.

The chunk map exists, although the bootstrap's chunk table has the ids,
because it pins the trust surface to this codebase's own format, is sorted for
one binary search per read, and is checked against the device table before
signing. (Converters once produced images whose chunk tables and inode
addresses disagreed while every chunk verified; checking ids alone cannot
catch that.)

**What is signed.** The RAFS component signs `source_image` (the OCI config
digest), `source_layers` (the diff IDs), `bootstrap {digest, size}`,
`chunk_map {digest, size}`, `device_size`, `format` and the producer key
identity, under the domain `ucloud.immutable-environment-rafs.v1`. Its
manifest in the environment repository has no layers. Packs, locators, tails,
layouts and the index are unsigned: a worker checks the chunk map's structure
(sorted, non-overlapping, every length at most 256 KiB and inside the device,
regions equal to the bootstrap's device table) and every chunk's digest, so
none of them can make it serve wrong bytes.

A RAFS component has one of two layouts (`chunk_store.mount_granularity` for
new conversions): `image`, one merged bootstrap per image (one component, one
mount), or `layer`, one bootstrap per layer stacked like layer components.
Workers read both. Sandbox builds need `image`.

### The index service

`ucloud-chunk-index` (`serve-chunk-index`,
[`chunk_index.py`](../ucloud_sandboxes/chunk_index.py)) keeps the SQLite index
(tables `chunks`, `packs`, `layers`, `roots`, `root_packs`). Everything in it
can be rebuilt from S3: pack footers are readable with one suffix range GET.
It runs on the store node when `store_node.serve_index` is set (its RAM holds
the index resident), otherwise on the gateway. It creates a write token and a
read token on first start. Writers (converters, RAFS builders, the
sandbox-build service) hold the write token and the S3 key; workers hold only
the read token.

| Endpoint | Use |
| --- | --- |
| `POST /v1/chunks/reserve` | Reserve up to 256 chunk ids for packing; each is known, reserved for this writer, or busy (another writer's 10-minute hold) |
| `POST /v1/chunks/commit` | Commit a pack after HEAD and footer checks, in one transaction |
| `POST /v1/chunks/locate` | Look up where chunks are stored |
| `POST /v1/layers/claim` | Claim a `(diff_id, converter)` layer conversion for 30 minutes, or get its finished bootstrap |
| `POST /v1/roots/register` | Register a component: every chunk must be present and not condemned; computes `root_packs`, the locator and blob layouts |
| `GET /v1/roots/...` | A registered component's stored locator |

### Write path

Conversion (`RafsConverter` in
[`chunk_convert.py`](../ucloud_sandboxes/chunk_convert.py)) is the same for
`convert-environment`, `chunk-migrate convert`, RAFS builders and sandbox
builds' stacking:

1. **Per layer:** claim it; if another conversion finished it, reuse its
   bootstrap. Otherwise run `nydus-image create` (v2.4.5,
   `--fs-version 6 --digester sha256 --compressor zstd --chunk-size 0x40000`,
   no chunk dictionary, never `--repeatable`, which zeroes owners). A layer
   tar out of depth-first path order is rewritten in order first, because
   nydus-image otherwise drops some whiteouts.
2. **Dedupe and pack:** reserve the layer's new ids, verify each new chunk,
   append it to a pack, PUT the pack (skipped if it exists) and commit it. A
   pack is durable in S3 before any row names it.
3. **Image:** merge the layer bootstraps (`--original-blob-ids`, at most 254
   blobs), derive the chunk map, upload both, sign the component and root.
4. **Verify** (`convert-environment --verify-device`, mandatory in
   `chunk-migrate`): mount the result through the worker's own device and
   compare the whole tree with the OCI layers: names, types, modes, owners,
   sizes, file hashes, symlinks, xattrs, hardlinks, whiteouts and file mtimes.
5. **Register** the component, then publish the root manifest last.

With `chunk_store.nydusd` set, conversions also write nydusd blob tables
(`--nydusd-blobs`), and a chunk stored in a different encoding than nydus
produced is re-stored in nydus's encoding, because nydusd reads blobs the store
node rebuilds from stored chunks. Every converted layer is checked against its
blob table before signing.

A crash at any step leaves nothing visible: unreferenced packs and chunks are
harmless and a rerun reuses them and yields the same root.

| Crash point | Left behind | Recovery |
| --- | --- | --- |
| Before a pack PUT | Local temporaries | Rerun |
| After a PUT, before its commit | A pack no row names | Harmless; a rerun re-reserves its chunks |
| After a pack commit, before the layer row | Committed chunks, an incomplete layer | A rerun claims the layer again and skips committed chunks |
| After commits, before registration | Committed, unreferenced chunks | A rerun reuses them |
| After the root manifest, before the gateway records it | An unrecorded root | Retention keeps it for the 72-hour recording window; a rerun yields the same digest |

Converters running at once share chunks across layers. Reservations make
each chunk packed once: a writer packs only the ids reserved to it, asks
again for busy ids after its own pack is committed (so writers never wait on
each other), and a dead writer's 10-minute hold lapses.

### Read path

Workers never query the index for chunks. At attach a worker fetches the
component's bootstrap, chunk map and locator in parallel and verifies the
first two against the signed component. With a store node, the locator names
store-node URLs and the worker holds only the read token; without one, it
carries SigV4-presigned S3 GET URLs (`url_ttl_seconds`), refreshed on a 403.
Locators and nydusd blob layouts are computed once at registration and stored,
because computing them on demand for 20 concurrent cold attaches saturated
the index. Reads then follow [Attach](#attach): served locally for the
bootstrap region, otherwise one binary search of the chunk map, a node-cache
lookup by chunk id (shared across images), and on a miss one GET per window
of the same pack. A chunk that fails verification refetches the locator once,
then reads as EIO.

### Retention

Nothing in the chunk store is deleted today: there is no garbage collector,
so packs, bootstraps, chunk maps and locators accumulate. Deduplication keeps
the growth to unique bytes; recipe and sandbox-built images that are never
used again keep their chunks.

The index is built for a mark-and-sweep collector (see
[roadmap.md](roadmap.md)) and already enforces its half of the protocol:
chunks and packs carry a `condemned` state; reservation treats a condemned
chunk as unknown, so it is packed again; and registration refuses a chunk map
that names a condemned or unknown chunk, so a root can never come to
reference a deleted chunk. Registration also counts every chunk in the tail
table of each of the root's nydusd blobs in `root_packs`, because nydusd and
block readahead read across chunks that only whiteout-hidden files use. Any
collector must mark from every live root (`image_roots`, durable owners,
routes, annotations, toolkits) plus a grace for recent packs, and hold
condemned packs longer than both the longest build-plus-registration and the
presigned-URL lifetime (`url_ttl_seconds`) before deleting them, since a
locator handed out earlier may still name them.

### The store node

`ucloud-chunk-store` (`serve-chunk-store`,
[`chunk_store_node.py`](../ucloud_sandboxes/chunk_store_node.py)) runs on a
dedicated store node on the private network (the VM init `store` role: no
Docker, node agent or heartbeat, so placement never sees it). Workers read
chunk-store objects only from it, never from S3, because S3's tail latency is
seconds and private workers reach it only through NAT.

- **Reads.** `GET /v1/objects/<key>` with one `Range`, for the content-addressed
  keys only, authenticated by the read token. nydusd reads blobs at
  `GET /v2/virtual/<component>/blobs/sha256:<blob>`, which the node assembles
  from stored chunks.
- **Fills.** A miss fills one aligned extent of `extent_bytes` from S3 with
  the node's own key. Concurrent misses on one extent join one fill. A GET
  that has made no progress for 3× the median time to first byte (150 ms to
  2 s) is hedged, at most twice; a slow but flowing GET is never duplicated.
  Retryable errors retry for 60 s, then the read answers 503.
- **Cache or replica.** By default the node is an LRU cache under
  `cache_bytes`. Reads by builders (write token) install first-to-evict and
  never promote, so conversions cannot evict a training run's working set;
  warm jobs promote. With `replica: true` the node mirrors the whole S3 prefix
  every `mirror_seconds`, never evicts, and refuses fills past `cache_bytes`.
  `data_device` puts the cache and index on a separate block device, so the
  server can be replaced without refilling. Every cached extent carries its
  sha256 in its file name and is rechecked on first use after a restart.
- **Native serving.** With `native_server_sha256`, the Go server
  `ucloud-chunk-serve` ([`runtime/chunk_serve`](../runtime/chunk_serve)) holds
  `listen`, answers reads of resident extents with sendfile on every core,
  and forwards misses and control requests to the Python node on loopback.
- **Prefetch.** `POST /v1/warm` (write token) fills objects or ranges behind
  demand fills; `GET /v1/warm/<job>` reports progress.
  `warm-chunk-store --component <digest>` warms a component's packs,
  metadata, tails and layouts.
- **Metrics.** `GET /v1/metrics` (hits, misses, fills, S3 requests, hedges,
  latency percentiles, cache bytes, evictions); `/healthz` is open.

**Availability.** Losing the store node loses no data; S3 is the store of
record. While it is down, workers cannot fetch chunks they lack, so cold
attaches of chunk-store images fail closed with EIO. Running sandboxes and
images already cached on a worker are unaffected. See
[operations-runbook.md](operations-runbook.md) for replacement.

The store node also runs the build package cache
(`ucloud-package-cache.service`, `serve-package-cache`) when
`store_node.package_cache` is set.

## The managed registry

The managed registry is a Docker Distribution instance
(`ucloud-sandbox-registry.service`) on the gateway host, reachable by builders
and nodes on the private network only. Its storage is a filesystem directory
on the gateway's data drive (`registry_store.kind: filesystem`) or S3
(`registry_store.kind: s3`). Clients never see registry coordinates: they
build and create by image id, and the gateway resolves ids to digest-pinned
references.

The chunk store holds the file data of training images; the registry holds
what is left:

- signed environment roots and component manifests (a few KB each), for RAFS
  and EROFS images alike;
- EROFS component blobs: toolkits, and images built with
  `builder_format: "erofs"`;
- OCI images that keep their manifests: builder outputs, imports, user builds
  and anything not converted;
- regenerated base copies (`ucloud-regenerated`), the shared BuildKit cache,
  and shared startup traces (`shared_traces`);
- park snapshots when `snapshot_store.kind` is `registry`.

### Retention

`ucloud-sandbox-registry-prune.timer` runs `registry-prune --execute` hourly:

- **Age.** Managed image tags whose last recorded sandbox use is older than
  `registry_retention_days` are deleted, keeping `registry_keep_per_repository`
  per repository (default 0). Tags with no recorded use are kept.
- **References.** The environment and snapshot repositories are retained by
  reference: a root is live while an image annotation, an `image_roots` row, a
  registered toolkit or composition, a route or a durable owner names it, and
  a component while a live root lists it. Anything else goes after
  `registry_reference_grace_seconds`; chunk-store roots get a 72-hour window
  to be recorded.
- **Leases.** `registry-usage.sqlite` holds durable references (routes,
  pushed builds, preparation owners such as `image-pool:` and
  `image-foundation:`) and finite leases (pulls, warmups). Every deletion is
  revalidated against them just before it runs. Each resolved digest also has
  a `ucloud-digest-sha256-<hex>` tag so collection sees it after its
  user-facing tag moves.

### Blob sweep and disk pressure

Deleting a manifest frees nothing until its blobs are swept. Physical
collection of the filesystem store stops the registry for the whole scan
(cold EROFS reads, builds, imports and snapshot publication stall; running
sandboxes continue), so scheduled runs only report that maintenance is due.
Run it in a window with admission paused:

```bash
sudo <gateway venv>/bin/python -m ucloud_sandboxes.systemd \
  registry-gc --allow-service-interruption --config /etc/ucloud-sandboxes/deployment.json
```

Blobs younger than `registry_blob_grace_seconds` survive. Prune, sweep and
pressure cleanup share one maintenance lock.

For the filesystem store, the gateway measures the registry volume:

- at `registry_disk_cleanup_percent`, the pressure timer reports that
  maintenance is needed; in a window,
  `registry-pressure --allow-service-interruption` prunes, sweeps, and if
  still above it evicts managed images least recently used first down to
  `registry_disk_target_percent`. Leased, routed, recently used and
  dispatched (chunk-store) images are never evicted.
- at `registry_disk_refuse_percent`, image builds and imports answer 503
  `registry_disk_pressure` (`Retry-After: 60`).

These guards measure the registry's own filesystem; size the thresholds to
the drive it is on.

## Node-side materialization

### The environment backend

Each worker runs a nodewide backend, `ucloud-environment-io.service`
(`serve-environment-io`), separate from the node agent so agent restarts never
detach mounted filesystems. It is reached only through a mode-0600 Unix socket
(`/run/ucloud-environment/io.sock`) with peer-UID checks, owns the NBD devices
and EROFS mounts, and shares the host mount namespace. If it dies with mounts
retained, its replacement refuses to adopt them; the affected sandboxes must be
drained first.

The Python node agent's `EnvironmentRootfsStore` resolves a root (dispatched,
else annotation), asks the backend to attach every component, composes them
and writes a receipt. The Rust node daemon (`noded`) creates sandboxes only
from images the Python agent has already materialized, found by receipt; it
asks the agent to materialize anything else.

### Attach

VM init loads the `nbd` module with 1,024 devices and never reconfigures a
loaded one. Each attach verifies the signed metadata, then binds an NBD device
that the kernel mounts as read-only EROFS. A device is shared by every image
that uses the component.

- **EROFS components** are served by the backend's Python NBD export. A miss
  fetches a complete 256 KiB chunk from the registry by HTTP range, checks it
  against the signed chunk index and keeps it in the node's verified chunk
  cache (`immutable_environments.cache_bytes`, at most half the node's
  reserved disk headroom). Concurrent readers of one chunk share one fetch;
  transient errors retry within a shared 30 s budget. Attaches run
  `attach_concurrency` at a time (default 1).
- **RAFS components** expose one address space: the bootstrap at offset 0 and
  each blob at its mapped address. With `chunk_store.nydusd`, each RAFS image
  is served by one stock `nydusd nbd` process (v2.4.5 built with
  `block-nbd`, pinned by sha256; the backend refuses any other binary). It
  reads the store node's virtual blobs with the read token, checks every
  chunk against the digests the signed bootstrap pins, and shares one file
  cache across all daemons on the node. Without nydusd, the backend's own
  RAFS device binary-searches the chunk map, fetches a miss as a window of up
  to 1 MiB of the same pack, verifies every chunk and installs its
  neighbours into the shared cache. RAFS attaches have 8 slots of their own.
- **Stalls are latency, not errors.** nydusd retries the store node for up to
  270 s per read and the NBD timeout is 600 s, so an S3 stall behind the
  store node delays a read but does not mark the device dead. A dead store
  node or daemon still reads as EIO within that bound, never as zeros.

### Composition and the writable layer

A root with one component (a RAFS `image`-layout image) is bind-mounted. A
root with several (layer groups, a toolkit) is a read-only OverlayFS of the
component mounts in order. Each sandbox gets its own copy-on-write upper over
that composition, plus tmpfs at `/tmp` and `/run`; writes such as `.pyc` files
or uv lock files land in the upper and never touch the shared layers.

Before disconnecting a component, the backend checks kernel mount dependencies,
including other overlays and binds; component leases fence concurrent
composition and collection.

### Idle image cache

Deleting a sandbox leaves its image's composition mounted for the next
sandbox of that image. The agent's reconciler collects idle compositions,
least recently used first, only while the node is over budget: attached
components above `device_budget_percent` (default 75) of its block devices, or
nydusd's shared cache above `cache_bytes`. It never collects an image with a
registration, an overlay user or a lease, or one used in the last 60 s. An
attach that finds no free device runs the sweep at once and retries once.

### Prefetch

Prefetch only orders fetches; every fetched chunk is still verified, and a
missing or invalid hint means demand loading. It applies to components the
backend serves itself (EROFS, and RAFS without nydusd).

- **Metadata hints** (EROFS): at attach, a job fetches the densest
  metadata-bearing chunks named by the signed hint, within 32 MiB and 30 s;
  the attach waits at most 5 s for it. With layout 2's metadata zone, `find /`
  after attach makes no remote reads. A RAFS image's bootstrap is its
  metadata and is local after attach.
- **Startup traces:** the first attach of a component records the chunks the
  guest reads first (30 s or 2,048 chunks) under
  `<backend root>/traces/`; later attaches replay them in the background
  (256 MiB, 120 s). With `shared_traces`, workers share traces through the
  registry, so a node that never attached a component replays another's.
- Prefetch holds at most a quarter of the miss slots and never starts while a
  demand miss waits.

`prefetch_enabled: false` starts the backend with `--disable-prefetch`. The
backend reads the setting once at start, so a change applies to newly
provisioned workers.

### Metrics

The backend's counters (cache hits and misses, downloaded bytes, corruptions,
fetch retries, active components, prefetch and trace counters) travel in each
worker heartbeat as `runtime_metrics.environment_io` and appear in the node's
`actual_usage`. The set of names is exact; gateways must accept a new set
before workers send it.

## Migrating registry images into the chunk store

`chunk-migrate` converts existing registry (EROFS) images into the chunk store
in waves and switches dispatch to the new roots. Each step is a dry run until
`--execute` where it deletes anything.

| Subcommand | Where | Does |
| --- | --- | --- |
| `inventory` | gateway | Read-only list of managed images: roots, bytes, family, build-input status, owners |
| `convert` | converter host | Convert and full-tree verify a wave (`--parallel`, `--verify-device`, `--shard`) |
| `record` | gateway | Record verified results as `converted` rows, after checking both roots and the annotation |
| `switch` | gateway | Dispatch the new roots (`dispatch_roots` must be on); warm each image first; re-point durable owners |
| `revert` | gateway | Dispatch the old roots again, from the journal |
| `release` | gateway | Drop durable owners on switched images' old EROFS closures |
| `verify-regeneration` | converter host | Check that roots regenerate their images' OCI trees exactly; writes receipts |
| `release-oci` | gateway | Delete released images' OCI manifests, remembering their tags (`--include-build-inputs` needs receipts and `regenerate_bases`) |
| `regenerate` | gateway | Push an OCI-released image's verified regeneration for builds |
| `release-originals`, `drop-staged` | gateway | Delete untagged pre-annotation manifests, and staged upstream sources |
| `status` | gateway | `image_roots` rows per wave and state |

Record converted results within 72 hours: retention deletes an unrecorded
chunk-store root after that window. Stop converters before maintenance on the
store node; a stopped converter can leak its NBD devices.

`unpack-environment --root <digest> --output-ref <ref>` regenerates a
one-layer OCI image from a chunk-store root (layout `image`), for rollback or
inspection; `--verify-image` compares instead of pushing. The tree is exact;
OCI digests are new, because the original layer boundaries and gzip bytes are
gone.

## Operator commands

| Command | Use |
| --- | --- |
| `image-index summary\|show\|list\|task-ids\|export-task-ids` | Read the image index; export `task_ids_file`s |
| `toolkit-register`, `toolkit-list` | Register a published toolkit image's root under `name:tag`; list registrations |
| `publish-environment` | Publish signed EROFS components for an existing OCI image (toolkits, canaries) |
| `convert-environment` | Convert a registry image into the chunk store |
| `unpack-environment` | Regenerate an OCI image from a chunk-store root |
| `chunk-migrate ...` | Migrate registry images into the chunk store (above) |
| `warm-chunk-store --component <digest> [--wait]` | Fill the store node with a component's objects before a burst |
| `registry-prune [--execute] [--evict-lru]` | Plan or run registry retention |
| `python -m ucloud_sandboxes.systemd registry-gc\|registry-pressure --allow-service-interruption` | Blob sweep and pressure cleanup, in a maintenance window |
| `provision-environment-key` | Create or recover the environment producer key pair |
| `serve-environment-io`, `serve-chunk-index`, `serve-chunk-store`, `serve-package-cache`, `serve-sandbox-builds` | Service entry points, run by their systemd units |

## Configuration reference

All image settings are in `deployment.json`; see
[deployment.md](deployment.md) for how it is rendered and converged.

### `immutable_environments`

Absent by default. When present, the gateway protects environment
dependencies; worker and builder adapters need their own switches.

| Key | Default | Meaning |
| --- | --- | --- |
| `trusted_keys_file` | required | Producer trust file (absolute path) |
| `signing_key_file` | `""` | Ed25519 private key; required for builders and sandbox builds |
| `repository` | `environments` | Registry repository for roots and components |
| `worker_enabled` | `false` | Workers run the environment backend |
| `builder_enabled` | `false` | Builders publish environments; needs `signing_key_file` and `allow_paths` |
| `allow_paths` | `[]` | Top-level paths EROFS publication includes; `*` for all but `dev`, `proc`, `sys`, `run` |
| `cache_bytes` | 1 GiB | Worker verified chunk cache; also the nydusd cache budget for idle-image collection |
| `preserve_mtimes` | `false` | Builders write EROFS layout 2 (needs erofs-utils 1.9) |
| `prefetch_enabled` | `true` | Metadata-hint and trace prefetch on workers |
| `attach_concurrency` | 1 | Concurrent EROFS component attaches per worker (1–256) |
| `device_budget_percent` | 75 | Idle images are collected only above this share of block devices |
| `dispatch_roots` | `false` | Gateway sets `environment_root` on every create |
| `regenerate_bases` | `false` | Gateway regenerates OCI copies of released bases for builds; needs `chunk_store.store_node` |
| `shared_traces` | `false` | Workers share startup traces through the registry |
| `builder_format` | `erofs` | `erofs` or `rafs`; `rafs` needs `builder_enabled` and `chunk_store.nydus_image_sha256` |
| `chunk_store` | absent | The chunk store (below) |
| `sandbox_builds` | absent | Sandbox builds (below) |

### `immutable_environments.chunk_store`

Every key except `store_node`, `nydusd` and `nydus_image_sha256` is required;
there are no defaults.

| Key | Meaning |
| --- | --- |
| `endpoint`, `bucket`, `region`, `prefix`, `force_path_style` | The S3 location |
| `access_key_id_env`, `secret_access_key_env` | Names of the environment variables holding the S3 key (read only by the index, writers and the store node) |
| `index_url`, `index_listen` | How clients reach the index service, and what it binds; with `serve_index`, `index_url` must name the store node |
| `index_database` | Index SQLite path (absolute) |
| `read_token_file`, `write_token_file` | Owner-only bearer token files (absolute) |
| `url_ttl_seconds` | Presigned URL lifetime, 3,600–604,800 |
| `mount_granularity` | `image` or `layer`, for new conversions |
| `nydus_image` | Path of `nydus-image` |
| `nydus_image_sha256` | Optional pin; required by `builder_format: "rafs"`, sandbox builds and RAFS builders |
| `concurrent_misses` | Worker miss slots for RAFS reads, 1–256 |
| `store_node` | Optional; the store node (below) |
| `nydusd` | Optional `{"path", "sha256"}`; serve RAFS images with nydusd; needs `store_node` |

### `chunk_store.store_node`

| Key | Default | Meaning |
| --- | --- | --- |
| `url` | required | How workers and the index name the node (`http://10.42.0.10:5091`) |
| `listen` | required | `host:port` it binds |
| `cache_dir` | required | Cache directory (absolute) |
| `cache_bytes` | required | Cache budget, 1 GiB–1 PiB |
| `extent_bytes` | required | Fill unit, a power of two, 1–64 MiB |
| `s3_concurrency` | required | Concurrent S3 requests, 1–512 |
| `serve_index` | required | Run the index service on this node |
| `replica` | `false` | Mirror the whole prefix and never evict |
| `mirror_seconds` | 600 | Replica mirror interval, 60–86,400 |
| `data_device` | absent | A `/dev/disk/by-id/` ext4 device holding the cache and index |
| `native_server_sha256` | absent | Pin for `ucloud-chunk-serve`; `listen` must not be loopback |
| `package_cache` | absent | The build package cache (below) |

### `store_node.package_cache`

| Key | Default | Meaning |
| --- | --- | --- |
| `listen` | required | `host:port` it binds |
| `url` | required | `http://` URL build sandboxes use as their proxy |
| `cache_dir` | required | Cache directory (absolute) |
| `max_bytes` | required | Package-file budget, at least 1 GiB |
| `upstreams` | required | Allowlisted host → origin serving the same paths |
| `index_seconds` | 600 | How long index files are served before refetch |
| `github_token_file` | `""` | Read-only GitHub token for the git proxy |

### `immutable_environments.sandbox_builds`

Requires `signing_key_file`, `dispatch_roots`, and a `chunk_store` with
`nydus_image_sha256` and `store_node`. When absent or disabled, every recipe
builds on the builders and `serve-sandbox-builds` exits with status 78.

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Build eligible recipes in sandboxes |
| `slots` | 8 | Concurrent sandbox builds, 1–64 |
| `stack_slots` | 3 | Concurrent stacking on the gateway host, 1–16 |
| `cpus` | 4.0 | Build sandbox CPUs |
| `memory_mb` | 8192 | Build sandbox memory (at least 1,024) |
| `disk_mb` | 32768 | Build sandbox disk (at least 4,096) |
| `timeout_seconds` | 1800 | Step budget per build, 60–14,400 |

### `builder`

| Key | Default | Meaning |
| --- | --- | --- |
| `max_nodes` | 1 | Builder VMs at most |
| `scale_down_idle_seconds` | 900 | Idle time before a builder scales down |
| `build_execution_timeout_seconds` | 1800 | Execution budget per build |
| `max_finishing_builds` | 0 | Extra publication/cleanup slots per builder, 0–2 |
| `buildx_cache_ref` | `""` | Shared BuildKit cache repository; empty disables it |
| `buildx_cache_max_bytes` | 32 GiB | Unique cache blob budget |
| `buildx_cache_max_entries` | 64 | Cache tags kept |
| `buildx_cache_max_age_seconds` | 604800 | Cache tag age limit |

### Registry

| Key | Default | Meaning |
| --- | --- | --- |
| `registry_store` | filesystem | `kind` (`filesystem` or `s3`) and its location |
| `registry_port` | 5000 | Registry port |
| `registry_retention_days` | 30 | Age-based tag retention |
| `registry_keep_per_repository` | 0 | Tags always kept per repository |
| `registry_reference_grace_seconds` | 3600 | Grace before unreferenced environment and snapshot manifests go |
| `registry_blob_grace_seconds` | 7200 | Grace before unreachable blobs are swept |
| `registry_disk_cleanup_percent` | 70 | Pressure cleanup threshold |
| `registry_disk_target_percent` | 60 | Eviction target |
| `registry_disk_refuse_percent` | 90 | Builds and imports refused above this |
| `registry_disk_gc_interval_seconds` | 1800 | Minimum interval between pressure sweeps |

### `upstream_mirror`

Absent by default.

| Key | Default | Meaning |
| --- | --- | --- |
| `listen_address` | required | Private IPv4 address (or `0.0.0.0` on a gateway with no public interface) |
| `storage_root` | required | Cache root; each upstream under `<storage_root>/<registry>` |
| `upstreams` | required | `[{registry, port, credentials_file?}]`; `credentials_file` is an owner-only env file with `REGISTRY_PROXY_USERNAME` and `REGISTRY_PROXY_PASSWORD` |
| `ttl_hours` | 168 | Cached content lifetime |
| `max_bytes` | 256 GiB | Total cache budget enforced by the hourly trim |
