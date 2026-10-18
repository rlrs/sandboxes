# Deployment

This document describes how a deployment is built, installed, configured and
upgraded. Day-to-day production procedures (backups, alerts, telemetry, node
replacement) are in [operations-runbook.md](operations-runbook.md). The
service's internal design is in [architecture.md](architecture.md).

The sandbox runtime is a Linux node stack: a pinned, patched gVisor `runsc`,
the direct Warden, and storage-native volumes. A compute provider only creates
and bootstraps the Linux machines that run it. UCloud is the primary provider;
Hetzner Cloud is a supported built-in alternative (see [Providers](#providers)).

## Components and machines

| Machine | How it is created | Runs | Durable state |
| --- | --- | --- | --- |
| Gateway VM | By an operator (`submit-vm --role gateway`) | Public gateway, placement worker, model relay, private registry and its maintenance timers, autoscaler, sandbox-build service, optional chunk index and upstream mirrors, PostgreSQL 18 | Gateway state directory (`data_root`), PostgreSQL, `/etc/ucloud-sandboxes` |
| Store node | By an operator, initialized with `init-vm --role store` | `ucloud-chunk-store`, optionally `ucloud-chunk-index` and `ucloud-chunk-serve` | Chunk-index database and the replica cache (S3 holds the permanent copy) |
| Sandbox workers | By the autoscaler | Direct gVisor Warden (`serve-direct-node-agent`), storage-native service, optional `ucloud-noded` front door | None |
| Builders | By the autoscaler | Image-only builder agent (`serve-builder-agent`), Docker and Buildx | None |

The store node exists only when `immutable_environments.chunk_store.store_node`
is configured; see [images.md](images.md). Workers and builders are
disposable: their disks hold runtime state, image caches and storage-native
caches only. The registry, the snapshot store and the chunk store are the
durable authorities for bytes; the gateway's databases are the authority for
routes and metadata.

### Gateway services

All gateway units ship inside the wheel (`ucloud_sandboxes/systemd/`) and run
as the `ucloud` service user with
`--config /etc/ucloud-sandboxes/deployment.json`.

| Unit | Purpose |
| --- | --- |
| `ucloud-sandbox-gateway.service` | `serve-control-plane`: the public and private HTTP API |
| `ucloud-sandbox-placement.service` | `serve-control-plane --placement-worker`: the durable placement executor |
| `ucloud-sandbox-relay.service` | `serve-model-relay` (see [model-relay.md](model-relay.md)) |
| `ucloud-sandbox-autoscaler.service` | `autoscaler --execute` |
| `ucloud-sandbox-registry.service` | Docker Distribution on `registry_port`, host network |
| `ucloud-sandbox-registry-prune.timer` | Reference retention, hourly |
| `ucloud-sandbox-registry-gc.timer` | Blob sweep, every six hours |
| `ucloud-sandbox-registry-pressure.timer` | Disk-pressure check, every minute |
| `ucloud-sandbox-snapshot-gc.timer` | Snapshot-store GC, daily; enabled only when `snapshot_store.kind` is `s3` |
| `ucloud-sandbox-builds.service` | Sandbox builds; exits 78 and stays stopped while `immutable_environments.sandbox_builds` is unset |
| `ucloud-sandbox-chunk-index.service` | Chunk index on the gateway; with `store_node.serve_index` it runs once to create the tokens and exits 78 |
| `ucloud-sandbox-upstream-mirror@<registry>.service` and `-trim.timer` | Pull-through mirrors, only with `upstream_mirror` |

## Durable state

| Path | Contents |
| --- | --- |
| `/etc/ucloud-sandboxes/deployment.json` | The exact deployment configuration |
| `data_root` (default `/work/data/ucloud-sandboxes/state`) | SQLite databases (`control-state.sqlite`, `routes.sqlite` or its PostgreSQL descriptor, `images.sqlite`, `autoscaler-state.sqlite`, `registry-usage.sqlite`, `model-relay.sqlite3`, …), credential files, `ssh/gateway-init`, `ucloud-session.json` |
| PostgreSQL database `ucloud` | Relay state and, after cutover, routing (`relay_postgres`) |
| `registry_store.data_root` | Registry blobs (filesystem store) |
| `/work/ucloud-sandboxes/gateway-venv` | The installed wheel |
| `node_package_root` (default `/work/ucloud-sandboxes/release`) | The sandbox and builder node bundles |

`data_root` may sit on the durable project drive (the default) or on the
gateway's local disk with scheduled backups to the project drive. Local disk
keeps SQLite and PostgreSQL commits off network storage; the production layout
does this (see [operations-runbook.md](operations-runbook.md)). Units that read
persistent state carry a `RequiresMountsFor` drop-in, so a missing mount stops
them instead of letting them start on an empty directory.

`control-state.sqlite` is the shared gateway and autoscaler authority for node
heartbeats and VM bootstrap retry state. `serve-control-plane`, `autoscaler`
and `heartbeats` must read the same file; there is no import path.

### PostgreSQL

The model relay requires `relay_postgres`; it refuses to start without it.
PostgreSQL 18 runs on the gateway and is reached over its Unix socket with peer
authentication, so the DSN file holds exactly
`host=/var/run/postgresql dbname=ucloud user=ucloud` and the database role
matches the service user.

Neither `deploy-all-in-one` nor the Hetzner gateway installer installs
PostgreSQL. On a fresh gateway, before the first reconcile:

1. Install `postgresql-18`, create role `ucloud` (`LOGIN`) and database
   `ucloud` owned by it.
2. Write the DSN to an absolute path readable only by `ucloud` and name it in
   `relay_postgres.dsn_file`.
3. Create the relay schema:
   `python -m ucloud_sandboxes.shared_control migrate --dsn-file <dsn> --deployment-id <id>`.
4. Optionally move routing to PostgreSQL with
   `python -m ucloud_sandboxes.shared_control.routing_cutover --routing-file <data_root>/routes.sqlite --dsn-file <dsn> --schema <schema>`.
   It runs offline with the gateway, relay and autoscaler stopped, copies every
   routing domain, and replaces `routes.sqlite` with a small descriptor
   (`ucloud-postgres-routing-v1`). Binaries that predate the cutover reject the
   descriptor rather than writing to a stale database, and a PostgreSQL failure
   never falls back to SQLite.

An existing SQLite relay must be imported explicitly
(`python -m ucloud_sandboxes.shared_control import-idle-relay --sqlite-file …`)
before the relay starts against PostgreSQL. `install_ucloud_gateway.sh`
restores PostgreSQL from a backup dump instead and applies the same server
tuning.

## Deployment configuration

`deployment.json` is one exact JSON object with `"schema": 5`. Unknown or
missing keys fail loading, and older schemas are rejected: construct a complete
current document rather than relying on migration. Print a starting point with:

```bash
uv run ucloud-sandboxes sample-config
```

Credentials never appear in the file. S3 keys are named by environment
variable, and the UCloud session is a separate file.

### Top-level keys

| Key | Default | Meaning |
| --- | --- | --- |
| `schema` | `5` | Required, exact |
| `deployment_id` | `"production"` | Identity carried in job labels, node bootstrap and every heartbeat; the gateway admits only matching jobs and heartbeats |
| `provider` | UCloud | Tagged compute-provider object; see [Providers](#providers) |
| `data_root` | `/work/data/ucloud-sandboxes/state` | Gateway state directory |
| `node_package_root` | `/work/ucloud-sandboxes/release` | Directory holding `sandbox-node-package.tar.gz` and `builder-node-package.tar.gz` |
| `gateway_private_host` | `"sandbox-gateway-production"` | Name workers use for the gateway and registry; use a DNS name, not an address |
| `registry_private_ip` | `""` | Optional registry alias address for workers |
| `gateway_port` | `8090` | Gateway HTTP port |
| `gateway_processes` | `1` | Public gateway processes sharing the port with `SO_REUSEPORT` (1–16, one host) |
| `gateway_create_placement` | `"ranked"` | `ranked` or `power_of_k`; see [scaling-policy.md](scaling-policy.md) |
| `gateway_heartbeat_ttl_seconds` | `120` | Heartbeat freshness bound |
| `gateway_max_concurrent_sandbox_creates` | `0` | Create concurrency bound (0: none) |
| `gateway_max_http_request_threads` | `1536` | Request threads, divided across `gateway_processes` |
| `heartbeat_interval_seconds` | `20` | Node heartbeat interval |
| `relay_port` | `8092` | Model relay port |
| `relay_request_timeout_seconds` | `7200` | Relay request deadline |
| `relay_worker_lease_seconds` | `600` | Relay worker lease |
| `relay_completed_request_retention_seconds` | `3600` | Replay window for completed results |
| `relay_postgres` | `null` | `{dsn_file, schema, max_connections, storage_budget_bytes}`; required by the relay |
| `registry_port` | `5000` | Registry port |
| `registry_store` | filesystem | `kind` `filesystem` (with `mount_point`, `data_root`) or `s3` |
| `registry_retention_days` | `30.0` | Age bound for retention |
| `registry_keep_per_repository` | `0` | Tags kept per repository regardless of age |
| `registry_disk_cleanup_percent` / `_target_percent` / `_refuse_percent` | `70` / `60` / `90` | Registry disk guard (below) |
| `registry_disk_gc_interval_seconds` | `1800` | Minimum spacing of pressure-triggered sweeps |
| `registry_reference_grace_seconds` | `3600` | Unreferenced objects younger than this stay |
| `registry_blob_grace_seconds` | `7200` | Blob collector grace for in-flight uploads |
| `snapshot_store` | `kind: registry` | Durable store for detached parks: `registry` or `s3` |
| `telemetry` | endpoint `""` | OTLP settings; empty endpoint disables export |
| `autoscaler_*` | see `sample-config` | Cycle interval and per-cycle bounds for init, deletes, detaches |
| `policy` | see [scaling-policy.md](scaling-policy.md) | Scaling policy |
| `sandbox` | see below | Sandbox worker pool and node runtime settings |
| `builder` | see below | Builder pool |
| `immutable_environments` | `null` | Immutable images and the chunk store; see [images.md](images.md) |
| `upstream_mirror` | `null` | Pull-through registry mirrors on the gateway |

`sandbox` carries the worker product and disk (`product_id`, `disk_gb`,
`docker_quota_image_gb`, `swap_gb`), the pinned `direct_runsc_commit`,
storage-native limits, `network_relays` (the names sandboxes use for the relay
and package cache; see [security.md](security.md)) and the node runtime
switches (`direct_*`). Most `direct_*` switches default off; their effects are
described in [node-admission-and-memory.md](node-admission-and-memory.md).
`builder` carries the builder product, disk, Docker quota, `max_nodes`,
`scale_down_idle_seconds`, build timeouts and the Buildx cache settings
(`buildx_cache_ref` names a registry tag on the gateway).

**Registry disk guard on UCloud.** The `registry_disk_*_percent` thresholds
measure `statvfs` of the registry's filesystem, so they only mean something on
a filesystem the registry owns. UCloud's `/work/data` is a shared drive whose
usage is mostly other projects'; the default thresholds would evict images at
once. Set all three to `100` there and bound the registry with
`registry_retention_days` and `registry_keep_per_repository`.

## Credentials

The deployment creates independent random secrets in `data_root`. Each
authorizes a different route set; never reuse one for another channel.

| File | Used by |
| --- | --- |
| `sandbox-api-token` | SDK clients: the least-privileged public routes |
| `gateway-token` | Operator tooling, the autoscaler and the relay's lifecycle bridge; stays on the gateway |
| `heartbeat-token` | Node heartbeat publication only |
| `node-control-token` | Gateway-to-node calls; every node route except `/healthz` |
| `relay-sandbox-token` | Sandboxes calling the relay |
| `relay-worker-token` | Inference workers polling the relay |
| `ssh/gateway-init` | SSH key the autoscaler uses to bootstrap nodes |

Clients receive the public HTTPS URL and the contents of `sandbox-api-token`
(and, for inference workers, the relay URL and `relay-worker-token`). The
gateway strips caller authorization headers before forwarding and adds the
node-control token itself. A node agent reads both node tokens once at start,
so a rotated heartbeat or node-control token takes effect when VM init replays
or the agent restarts; rotate the heartbeat token on the gateway and nodes
together. See [security.md](security.md) for the route split.

Register the gateway's SSH public key with UCloud so new VMs accept it:

```bash
uv run ucloud-sandboxes ensure-ucloud-ssh-key \
  --config /etc/ucloud-sandboxes/deployment.json \
  --session-file <data_root>/ucloud-session.json \
  --public-key-file <data_root>/ssh/gateway-init.pub
```

## Release artifacts

A release consists of the Python wheel, the native runtime components, and two
role-specific node bundles. Every artifact is pinned by digest; nodes never
contact a package repository.

### Wheel

```bash
uv build                                   # dist/ucloud_sandboxes-<version>-py3-none-any.whl
python scripts/verify_installed_wheel.py   # in a venv with the wheel installed
```

The gateway installs the wheel with the `postgres` extra.

### Runtime components

| Component | Source | Build |
| --- | --- | --- |
| Patched `runsc` | `runtime/gvisor/` patches over a pinned gVisor release | `runtime/gvisor/build_pinned.sh GVISOR_CHECKOUT OUTPUT_DIRECTORY` |
| Managed PID 1 (`ucloud-sandbox-init`) | `runtime/managed_process/` (Go) | `runtime/managed_process/build.sh [OUTPUT]` |
| Storage-native backend | AgentEnv plus `runtime/storage_native/*.patch` | `runtime/storage_native/build_pinned.sh AGENTENV_CHECKOUT OUTPUT_DIRECTORY` |
| `ucloud-noded` node front door | `runtime/noded/` (Rust) | `runtime/noded/build_pinned.sh OUTPUT_DIRECTORY` |
| `ucloud-chunk-serve` store-node reader | `runtime/chunk_serve/` (Go) | `runtime/chunk_serve/build_pinned.sh OUTPUT_DIRECTORY` |
| `nydusd` with block-nbd | Pinned nydus release | `runtime/nydusd/build_pinned.sh NYDUS_CHECKOUT OUTPUT_DIRECTORY` |

Each build is deterministic for one commit and toolchain, and writes a build
manifest with the artifact digest. The storage-native build manifest pins the
backend, its provenance, patches and license.

### Node bundles

Both bundles contain a manifest (role, platform, packages, kernel modules,
artifact digests), an offline Debian package closure (Docker, containerd, XFS
and host tools), a preassembled Python agent runtime, and the exact
host-kernel module closure (`RUNTIME_KERNEL_MODULES` in
[`vm_init.py`](../ucloud_sandboxes/vm_init.py)). The sandbox bundle adds
`runsc`, managed PID 1, the storage-native backend, and optionally `nydusd`,
`ucloud-chunk-serve` and `ucloud-noded`. The builder bundle adds Buildx and
omits the sandbox-only artifacts. Each bundle has a `.sha256` sidecar.

There are three ways to produce them:

- **From scratch.** `deploy-all-in-one` assembles both bundles on the gateway
  from the wheel, `runsc`, managed PID 1 and the storage-native manifest. It
  stops if any input cannot be built, downloaded or verified.
- **Repack.** When the OS package set and kernel are unchanged,
  `scripts/repack_node_bundle.py --source <old sandbox bundle> --wheel <wheel> --storage-backend … --storage-manifest … --storage-license … --output …`
  replaces the agent and storage backend and keeps every other qualified
  artifact. `--direct-runsc`, `--direct-runsc-commit` and `--managed-init`
  replace the runtime together; `--kernel-release` with `--kernel-module-dir`
  replaces the kernel closure; `--agent-dependency-wheel` (repeatable) or
  `--agent-runtime-root` supply new Python dependencies. It validates every
  retained artifact and fails rather than produce a partially importable agent.
- **Derive the builder.**
  `scripts/derive_builder_bundle.py --source <sandbox bundle> --buildx-deb <deb> --output <builder bundle>`
  keeps the shared OS and kernel closure, drops the sandbox-only artifacts and
  adds Buildx.

Never keep an old kernel closure only because the OS release name is
unchanged: distributions advance kernels within a release, and a bundle's
modules match exactly one.

Every release repacks both bundles with the new wheel. Placement only sends new
work to nodes whose agent version equals the gateway's package version, so a
gateway upgrade without new bundles would leave the fleet unschedulable.

## First deployment on UCloud

1. **Create the gateway VM.** Render a job, inspect it, then submit it:

   ```bash
   uv run ucloud-sandboxes submit-vm --config deployment.json \
     --role gateway --mount <project-drive-path> --hostname <gateway-host> \
     --output json
   ```

   The VM must join the private network (`provider.private_network_id`), mount
   the project drive at `/work/data`, and bind public links to the gateway
   port and the relay port (`vm-public-link-attachment` renders the fragment).
   The default product is `cpu-amd-zen5-4-vcpu`; pass `--product-id` for
   another. `submit-vm` sets a time allocation (1 h by default); remove
   `timeAllocation` from the payload for a long-lived VM. Add `--execute` only
   after reviewing the payload.

2. **Install PostgreSQL** as described in [PostgreSQL](#postgresql).

3. **Converge the gateway.** Render the remote script first:

   ```bash
   uv run ucloud-sandboxes deploy-all-in-one <gateway-job-id> \
     --config deployment.json \
     --wheel dist/ucloud_sandboxes-<version>-py3-none-any.whl \
     --direct-runsc <path>/ucloud-direct-runsc \
     --managed-init <path>/managed-init \
     --storage-native-manifest <path>/storage-native-manifest.json \
     --output script
   ```

   With `--execute` it stages the release under `/work/ucloud-sandboxes/release`,
   builds and verifies both node bundles, installs the wheel into
   `/work/ucloud-sandboxes/gateway-venv`, copies the UCloud session into
   `data_root` (unless `--no-copy-session`), creates any missing credentials
   and the SSH bootstrap key, installs `deployment.json` and the systemd units,
   registers the SSH key, opens the configured public links (unless
   `--no-open-public-links`), and finishes with `gateway-reconcile`. Rerunning
   it keeps existing credentials.

4. **Install operations tooling**: `scripts/install_ucloud_ops.sh` (backups,
   health watch) and `scripts/install_ucloud_observability_stack.sh`; see
   [operations-runbook.md](operations-runbook.md).

5. **Create the store node** if the chunk store uses one; see
   [operations-runbook.md](operations-runbook.md#store-node).

6. **Verify** before accepting traffic:

   ```bash
   curl -fsS https://<gateway-public-url>/healthz      # reports the package version
   curl -fsS https://<relay-public-url>/healthz
   curl -i  https://<gateway-public-url>/v1/sandboxes  # 401 without a key
   curl -fsS -H "Authorization: Bearer $(cat <data_root>/sandbox-api-token)" \
     https://<gateway-public-url>/v1/sandboxes
   ```

   Also check the registry's `/v2/` on `registry_port`, `systemctl` state of
   the units above, the autoscaler journal, and one complete build-push and
   sandbox create.

`scripts/install_ucloud_gateway.sh` is the other installer: it rebuilds a
gateway from a backup on a fresh VM (see
[operations-runbook.md](operations-runbook.md#when-something-is-lost)).

### Service convergence

`python -m ucloud_sandboxes.systemd gateway-reconcile --config /etc/ucloud-sandboxes/deployment.json`
is the one service-convergence routine for every provider. It reloads systemd,
enables the registry timers, enables or disables the snapshot-GC timer by
`snapshot_store.kind`, enables or disables the chunk index and upstream mirrors
by configuration, restarts the registry and waits for `/v2/`, restarts
placement, gateway, relay, autoscaler and builds, and waits for the gateway and
relay `/healthz` (and the chunk index's when it runs on the gateway). Run it
as root after any change to the wheel, units or `deployment.json`. Installers
do not carry their own restart order or health logic.

`ucloud_sandboxes.systemd` also provides `registry`, `registry-gc`,
`registry-pressure`, `registry-recover` (restart a registry stopped by an
interrupted collection), `upstream-mirror` and `upstream-mirror-trim`, which
the units call.

## Upgrades

Upgrade the gateway first, then let workers turn over.

1. Build the wheel, repack the sandbox bundle and derive the builder bundle
   (above). Stage them in a new release directory on the gateway.
2. Take a gateway backup (`backup_gateway_state.py gateway`, see the runbook).
3. Stop `ucloud-sandbox-registry-prune.timer` for the duration.
4. Install the wheel into the gateway venv:
   `/work/ucloud-sandboxes/gateway-venv/bin/python -m pip install --force-reinstall '<wheel>[postgres]'`.
   Copy changed unit files from the package's `systemd/` directory to
   `/etc/systemd/system/`.
5. Point `node_package_root` at the new release directory and apply any
   configuration changes the release notes in `CHANGELOG.md` call for. If the
   release adds PostgreSQL tables, apply them before restarting (the relay
   schema with `python -m ucloud_sandboxes.shared_control migrate`; routing
   additions through `PostgresRoutingStore.migrate()`).
6. Run `gateway-reconcile`, confirm `/healthz` reports the new version, and
   start the prune timer again.
7. Workers and builders take the new bundle at their next VM init. Existing
   nodes keep serving their sandboxes but receive no new placements; drain
   them or let idle scale-down replace them.

Rerunning `deploy-all-in-one --execute` is also a valid upgrade; it rebuilds
the bundles from scratch and keeps credentials.

The gateway accepts the previous worker heartbeat shape, while an older gateway
rejects unknown protocol fields. Do not roll back only the gateway while newer
workers remain admitted. To roll back, reinstall the previous wheel and
configuration and reconcile; if the failed release changed database contents,
restore the pre-upgrade backup.

Changing `gateway_create_placement` takes effect only when the gateway and
placement units restart together, which `gateway-reconcile` does.

Managed hosts never upgrade OS packages on their own. Gateway installers, VM
init and store init write `/etc/apt/apt.conf.d/99zz-ucloud-no-unattended-upgrades`
and mask the APT daily timers and `unattended-upgrades.service`, so an
unattended library update cannot restart PostgreSQL, the registry or the
control plane during a run. Package updates are an explicit maintenance task.

## VM initialization

VM init turns a running instance into a sandbox node, a builder or a store
node. It is an authenticated bootstrap step that the autoscaler owns for
workers and builders; it is not a general host installer. `init-vm` replays the
same bootstrap for one running VM:

```bash
ucloud-sandboxes init-vm <job-id> --config /etc/ucloud-sandboxes/deployment.json \
  --role sandbox|builder|store --package-spec <bundle> \
  --ssh-private-key-file <data_root>/ssh/gateway-init [--execute]
```

It requires a local bundle path and does not accept a URL or installer
specification. Without `--execute` it prints the plan.

### Inputs

Each init receives the deployment, job and node identity and the advertised
node URL; the heartbeat and node-control tokens; the role bundle path and its
SHA-256; physical CPU, memory and disk; Docker image-cache and swap sizes; the
private registry settings; and, for sandbox nodes, the `runsc` commit,
storage-native and snapshot-store settings, network policy, telemetry settings
and the immutable-environment settings.

### Bundle staging and verification

The autoscaler stages the bundle over SSH into
`/var/cache/ucloud-sandboxes/init-packages/<sha256>/` and passes the digest to
VM init. A matching root-owned runtime receipt there skips the transfer; a
root-owned bundle and sidecar at the same content-addressed path is reused;
otherwise the bundle is streamed once.

On a fresh image VM init re-hashes the archive, extracts it into a
digest-keyed directory and validates every declared file, aborting on any
mismatch. After a successful static install it records a root-owned receipt.
A golden-image clone whose role, init version, kernel, digest, receipt and
installed artifacts all match skips transfer, extraction and installation. VM
init never enables a package repository or substitutes an artifact from the
network. A new release misses the receipt and takes the full verified install.

### Sequence

1. Validate identity, tokens, role, resource accounting, network policy and
   bundle digest.
2. Create the service user; install the token files with mode `0600`.
3. Verify the bundled runtime; install only missing host packages from its
   offline closure; install the runtime payloads and kernel modules. Already
   configured packages are not upgraded and do not trigger initramfs
   regeneration.
4. Provision bounded swap and the Docker image-cache filesystem (growing a
   smaller snapshot-baked store to the configured size).
5. On sandbox nodes, install `runsc`, managed PID 1 and the storage-native
   backend, and create its cache and device-pool services.
6. Configure Docker for image operations and the private registry.
7. Activate the bundled Python runtime and write the role's node unit.
8. Remove any heartbeat timer an older release installed, start the node and
   require `/healthz`.

A failed prerequisite stops init before the node agent starts. The gateway
treats a node as unavailable until a fresh authenticated heartbeat reports the
expected deployment and versions.

### Roles

- **Sandbox nodes** run `serve-direct-node-agent` as root in
  `ucloud-sandbox-node.service`, because the Warden owns OCI bundles,
  namespaces, cgroups, storage-native mounts and `runsc` processes.
  `ucloud-storage-native.service` is a required dependency. When
  `sandbox.direct_node_front_door` is set, `ucloud-sandbox-noded.service` runs
  in front of the agent; it is off by default.
- **Builders** run `serve-builder-agent` as the service user with Docker group
  membership. They expose image cache, pull, build, heartbeat, health and
  drain, and push gateway-assigned tags to the private registry with Buildx.
  They carry `ucloud-sandboxes/builder=true` and never the sandbox-node label.
- **Store nodes** use only the bundle's agent runtime: no Docker, node agent or
  heartbeats, so placement never sees them. Store init writes
  `/etc/ucloud-sandboxes/chunk-store.json` and the root-only
  `chunk-store.env` (the chunk-store S3 key from the environment variables
  `chunk_store` names), installs both index tokens, mounts
  `store_node.data_device` at `/mnt/store-replica` when set, enables
  `ucloud-chunk-store.service` (plus `ucloud-chunk-index.service` with
  `serve_index` and `ucloud-chunk-serve.service` with `native_server_sha256`)
  and fails unless `/healthz` answers.

### Heartbeats

The node agent sends its own heartbeats from one thread. The node unit passes
`--heartbeat-url`, `--heartbeat-bearer-token-file`,
`--heartbeat-interval-seconds` (from `heartbeat_interval_seconds`) and one
`--heartbeat-label` per provider label.

- The first heartbeat goes out as soon as the agent serves; sending stops when
  serving stops.
- After a delivery the next waits one interval. After a transport or sampling
  error, 408, 429 or 5xx, retries start at 1 s and double up to one interval.
  Any other rejection waits a full interval. Every wait is jittered ±20%.
- Each attempt samples a fresh heartbeat, the same one `GET /v1/heartbeat`
  returns, with the provider labels merged in.

Every VM init run re-renders the node unit and restarts the agent, so a new
release always starts with the current sender flags.
`scripts/upgrade_owned_builder.py` is a narrower tool for one idle
qualification builder: it swaps only the runtime and keeps the unit's command
line.

### Diagnostics

The init script prints one line per completed phase:

```text
UCLOUD_INIT_PHASE name=runtime-bundle duration_ms=17321 total_ms=19002
```

A failure is terminal for that attempt. Inspect the init output and the
`ucloud-sandbox-node`, `ucloud-storage-native`, Docker and containerd
journals, fix the bundle or configuration, and replay the same bootstrap. Do
not repair a node by installing a different artifact by hand.

## Autoscaler operation

The autoscaler is dry-run by default. A single `--execute` authorizes provider
creates and stops, route reconciliation and VM initialization; the systemd unit
passes it. Preview one cycle without changing anything:

```bash
ucloud-sandboxes autoscaler --once --config /etc/ucloud-sandboxes/deployment.json --output json
```

Discovery accepts only jobs that carry the deployment label
(`ucloud-sandboxes/deployment`) and an explicit sandbox or builder ownership
label (`ucloud-sandboxes/node` or `ucloud-sandboxes/builder`); names never
establish ownership. `--include-job <id>` inspects a known job outside that
filter, and `--jobs-file` supplies an offline inventory fixture. Provider
mutations are journaled. Automatic termination targets only jobs with the
exact deployment and role labels.

A drain closes admission before the node reports progress. The autoscaler stops
a node only after a fresh authenticated heartbeat confirms the matching drain
token, a complete empty inventory and zero owned resources. A worker suspended
after it started is treated as lost: its disk and processes are not trusted
again, and replacement capacity comes from durable demand. See
[scaling-policy.md](scaling-policy.md).

## CLI reference

`ucloud-sandboxes` (`ucloud_sandboxes/cli.py`) subcommands:

| Command | Purpose |
| --- | --- |
| `sample-config` | Print a complete starter `deployment.json` |
| `submit-vm` | Render or submit a UCloud VM job (`--role node|gateway|builder`) |
| `vm-network-attachment` | Render the private-network job fragment |
| `vm-public-link-attachment` | Render a public-link job fragment |
| `open-vm-web` | Open a VM web session for a public-link port |
| `inspect-job` | Inspect one UCloud job |
| `ensure-ucloud-ssh-key` | Register the gateway's SSH key with UCloud |
| `deploy-all-in-one` | Install or upgrade a deployment on a gateway VM |
| `init-vm` | Plan or execute VM init for one VM (`sandbox`, `builder`, `store`) |
| `autoscaler` | Plan or run the autoscaler |
| `heartbeats` | List stored node heartbeats |
| `registry-prune` | Plan or run registry retention (`--evict-lru` above the cleanup threshold) |
| `serve-control-plane`, `serve-model-relay`, `serve-direct-node-agent`, `serve-builder-agent` | Service entry points |
| `serve-chunk-index`, `serve-chunk-store`, `serve-package-cache`, `serve-environment-io`, `serve-sandbox-builds` | Chunk-store and image service entry points |
| `warm-chunk-store` | Fill the store node with components ahead of a burst |
| `convert-environment`, `unpack-environment`, `chunk-migrate` | Chunk-store conversion and migration |
| `publish-environment`, `provision-environment-key` | Immutable-image publication and signing keys |
| `toolkit-register`, `toolkit-list` | Toolkit registration |
| `image-index` | The gateway's index of training image names |

Commands that call UCloud accept `--session-file` as a local credential
override; the session is never stored in `deployment.json`. The wheel also
installs `ucloud-sandboxes-storage` (the node storage service),
`ucloud-sandboxes-snapshot-gc` and `ucloud-sandboxes-registry-migrate`.

## Providers

### The provider boundary

`ucloud_sandboxes.providers.base.ComputeProvider` is the complete autoscaler
boundary. An adapter:

- lists, decodes and retrieves instances as `ProviderInstance` values;
- normalizes native lifecycle states to `PROVISIONING`, `RUNNING`,
  `UNAVAILABLE`, `LOST` or `TERMINAL`;
- decides whether an instance belongs to the configured pool scope;
- renders semantic sandbox and builder `InstanceCreateIntent` values into a
  native create request, preserving every intent label and returning it in
  `ProviderInstance.labels` (the operation-recovery identity);
- reports create and terminate outcomes as accepted, rejected or uncertain;
- provides bootstrap access (an SSH command) for a running instance;
- declares `requires_continuity_history`, `requires_guest_continuity` and
  any `destructive_instance_losses` it can prove.

Policy, drain safety, operation journaling, heartbeats, routing, node roles,
runtime installation and sandbox lifecycle stay outside the adapter. The core
never branches on a provider's native states and never builds a provider
payload.

Provider settings are one strictly tagged object, `{"kind": …, "scope_id": …,
…}`. Each built-in provider rejects unknown keys.

### UCloud

UCloud is the primary provider and the only one with in-tree deployment
automation (`submit-vm`, `deploy-all-in-one`, the `install_ucloud_*`
scripts).

| Key | Meaning |
| --- | --- |
| `scope_id` | UCloud project ID |
| `private_network_id` | Private network every VM joins |
| `template_job_id` | Optional job to copy settings from |
| `gateway_public_link_id` | Public link bound to the gateway |
| `gateway_public_link_port` | Gateway port behind the link (default `8090`) |

The adapter discovers each VM's SSH command from job updates and bootstraps as
the `ucloud` user, so init scripts reach root-only paths through `$SUDO`.
Private addresses change when UCloud restarts a VM; refer to the gateway and
store node by DNS name everywhere (`gateway_private_host`, `store_node.url`,
`network_relays`, `telemetry.endpoint`).

### Hetzner Cloud

The built-in `hetzner` provider autoscales sandbox and builder nodes from
system images or snapshots with private-IP SSH bootstrap and ambiguity-safe
create and delete. The gateway and the network foundation are provisioned
separately.

```json
{
  "kind": "hetzner",
  "scope_id": "sandboxes",
  "api_token_env": "HETZNER_API_KEY",
  "network_id": 1234567,
  "location": "hel1",
  "sandbox_server_type": "cpx62",
  "sandbox_image": 23456789,
  "builder_server_type": "cpx62",
  "builder_image": 23456790,
  "ssh_user": "root",
  "ssh_key_ids": [3456789],
  "firewall_ids": [4567890],
  "enable_ipv4": false,
  "enable_ipv6": false,
  "enable_private_egress": true,
  "private_dns_servers": ["1.1.1.1", "8.8.8.8"]
}
```

| Key | Default | Meaning |
| --- | --- | --- |
| `scope_id` | required | Stable local name for the Hetzner project; the token selects the real project |
| `api_token_env` | `HETZNER_API_KEY` | Environment variable holding the API token, read lazily and never stored |
| `api_base_url` | `https://api.hetzner.cloud/v1` | API origin (HTTPS) |
| `network_id` | required | Private network; its address is the SSH target and advertised node address |
| `location` | `hel1` | Location |
| `sandbox_server_type`, `builder_server_type` | `cpx62` | Server types |
| `sandbox_image`, `builder_image` | required | Image or snapshot ID, or a system image name such as `ubuntu-26.04` |
| `ssh_user` | `root` | Bootstrap user |
| `ssh_key_ids` | required, ≥1 | Keys injected at create, so bootstrap never depends on a root password |
| `firewall_ids` | `[]` | Required when either public address family is enabled |
| `placement_group_id` | `null` | Optional placement group |
| `enable_ipv4`, `enable_ipv6` | `false` | Public addresses |
| `enable_private_egress` | `false` | Default route through the network router, mutually exclusive with public addresses |
| `private_dns_servers` | `1.1.1.1`, `8.8.8.8` | Static resolvers written with private egress |

**Networking.** Workers default to private-only. With
`enable_private_egress`, cloud-init installs a default route through the
Hetzner network router and writes a static resolver list before SSH bootstrap,
because a private-only DHCP lease supplies no DNS. The network must route
`0.0.0.0/0` to the gateway's private address, and the gateway must forward and
masquerade that traffic. Hetzner Cloud firewalls do not filter private
traffic, so the provider omits them from private-only creates and the guest
nftables policy remains authoritative.

**Sizing.** Hetzner states disk in decimal GB; the provider converts to whole
binary GiB before resource accounting (640 GB becomes 596 GiB). Advertise
schedulable memory a little below the catalog figure, since the guest sees
less than nominal.

**Images.** Boot workers from a snapshot, not the plain system image: a
system image boots whatever kernel it currently carries, and the bundle's
modules match only one. A snapshot holds the slow, static part of init (the
tested kernel and modules, host packages, one verified content-addressed
bundle and its receipt) and no deployment secrets; VM init still runs after
boot to install identity, tokens and dynamic configuration. Build it from a
disposable source server, never an active worker:

1. Initialize the source with the release bundle and pass a lifecycle canary.
   Drain it before anything else.
2. Run `scripts/prepare_hetzner_snapshot.sh "$(hostname)" <docker-image-gib> sandbox|builder`
   on the source after stopping the node services. It refuses to run on a host
   whose name does not match, removes workload state, credentials, Docker data,
   logs and machine identity, keeps exactly one verified runtime bundle and
   receipt, installs the private-network wait-online override, and runs
   `cloud-init clean` so clones get fresh identity and host keys.
3. Power the source off and snapshot it. Snapshot and server architectures must
   match, and a snapshot boots only on a disk at least as large as its source.
4. Boot one canary from the snapshot and verify fresh host keys, the private
   address, no stale heartbeat, idempotent init and a create, exec, park and
   wake cycle.

`scripts/qualify_hetzner_host.sh` is a destructive host qualification for a
disposable VM. The Ubuntu 26.04 kernel on Hetzner lacks `xt_tcpudp`, so Docker
host-port publishing (`docker run -p`) fails there; the direct runtime does not
use it.

**Foundation.** `scripts/setup_hetzner_foundation.py` idempotently creates the
private network, the bootstrap SSH key, a deny-inbound worker firewall and a
gateway firewall (HTTP(S) only unless `--gateway-ssh-source-cidr` is given). It
is dry-run without `--execute`. `--egress-gateway-ip` adds the network's
default route to the gateway; that route does nothing until the gateway's
forwarding and NAT rules are active.

**Gateway.** `scripts/install_hetzner_gateway.sh --public-ip <ipv4> [--volume-device <path>]`
installs the gateway from inputs staged in `/tmp` (wheel, `deployment.json`,
both node bundles, the SSH bootstrap key pair, the Hetzner environment file and
`configure_hetzner_sdk_ingress.sh`), sets up NAT for private workers, and
finishes with `gateway-reconcile`. `--volume-device` is needed only for a
filesystem registry store. PostgreSQL must be prepared first (see
[PostgreSQL](#postgresql)).

`scripts/configure_hetzner_sdk_ingress.sh --public-host <dns-or-ip> [--email <address>]`
puts Nginx in front of the loopback gateway port, publishes the relay under
`/relay/`, and obtains a Let's Encrypt certificate, including an IP-address
certificate when no domain exists; it installs a twice-daily renewal timer.
Never expose port 8090, the registry port or the relay's private port
publicly.

`scripts/install_hetzner_otlp_stack.sh` installs the telemetry stack with
Tempo writing trace blocks to S3 (`--s3-endpoint`, `--s3-bucket`,
`--s3-region`, `--s3-prefix`, `--credentials-env-file`), with the WAL and
metrics on `--data-root`.

### Adding a provider

Implement `ComputeProvider` and expose a factory
`factory(configuration, cli_options) -> ComputeProvider`. Register it under
the configuration `kind` in the entry-point group
`ucloud_sandboxes.compute_providers`:

```toml
[project.entry-points."ucloud_sandboxes.compute_providers"]
examplecloud = "examplecloud.sandboxes:build_provider"
```

The loader rejects a missing or duplicate registration, a factory whose
provider reports another `kind`, and a provider without boolean
`requires_continuity_history` and `requires_guest_continuity`. Keep
cloud-specific options in the tagged configuration; no policy, reconciliation,
routing, registry or runtime module changes. Test the adapter against a fake
of the same protocol: lifecycle normalization, pool eligibility, request
rendering, ambiguous mutation recovery and bootstrap access.

The adapter solves compute only. A deployment on another cloud also needs:

- Linux VMs where the bootstrap user can run privileged installation;
- private reachability from the gateway to node-agent and SSH endpoints;
- the kernel modules the bundle carries, including `ublk_drv`, `erofs`,
  `nbd`, `nft_log`, `nfnetlink_log` and `virtiofs`; verify them on an image
  without a full modules package before admitting a worker;
- durable gateway state and registry storage, and PostgreSQL 18 on the gateway;
- a way to distribute the node bundle and credentials;
- stable node identities and enough labels or metadata to recover operations
  after ambiguous API responses;
- its own deployment automation; `deploy-all-in-one` and the UCloud helpers
  are UCloud-specific, but the gateway and worker services are the same.

Object storage need not come from the compute provider: the chunk store and
snapshot store take any S3-compatible endpoint.
