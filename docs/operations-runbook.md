# Production runbook (UCloud)

This runbook covers operating the production deployment on UCloud: where
things live, backups, alerts, telemetry, the snapshot and chunk stores,
releases, and recovery when a machine is lost. How the deployment is built and
installed is in [deployment.md](deployment.md).

Production-specific values (project, hostnames, bucket, drive paths) are in the
deployment configuration, `/etc/ucloud-sandboxes/deployment.json` on the
gateway, and in the backups. This document refers to them by role.

## Layout

| Part | Where | Durable state |
| --- | --- | --- |
| Gateway | UCloud VM job, private network, project drive mounted, two public links (gateway and relay) | Local disk: PostgreSQL 18 and the state directory `/var/lib/ucloud-sandboxes/state` (`data_root`) |
| Store node | UCloud VM job, private network, no public link | Local disk: chunk index and replica |
| Workers and builders | Autoscaled VM jobs | None |
| Registry | `registry_store.data_root` on the project drive (`/work/data`) | Project drive |
| Chunk store | S3 bucket and prefix named in `immutable_environments.chunk_store` | Permanent |
| Backups and release kits | Directories on the project drive | Project drive |

The gateway's and store node's local disks end with their jobs. Everything on
them is backed up to the project drive, and the S3 bucket is the permanent copy
of every environment.

**Addresses.** Every component names the gateway and the store node by DNS
name, which UCloud's resolver answers on every VM of the project (short names
work through the search domain). Never put a private IPv4 address in the
configuration: UCloud changes a VM's private address when it restarts the VM.
The names appear in `gateway_private_host`, `builder.buildx_cache_ref`,
`sandbox.network_relays`, `telemetry.endpoint` and `chunk_store.store_node.url`.
Services bind `0.0.0.0`; the VMs' only other interfaces are loopback and
Docker's. Sandboxes reach the relay and the package cache through
`sandbox.network_relays` (`default`, `packages`), which their `/etc/hosts` maps
to stable virtual addresses that workers translate (see
[security.md](security.md)). Clients name the relay's host in their
`guest_relay_url`.

## Backups

`scripts/install_ucloud_ops.sh --backups <dir on /work/data> [--public-gateway URL] [--public-relay URL]`
installs `scripts/backup_gateway_state.py` and `scripts/ops_watch.py` under
`/usr/local/libexec/ucloud-sandboxes/` with their timers. It refuses a backup
directory outside `/work`.

| Timer | Contents | Kept |
| --- | --- | --- |
| `ucloud-sandbox-backup-gateway` (hourly) | `postgres.dump` (`pg_dump` custom format), `state/` (online copies of every SQLite database, tokens, SSH and producer keys, the UCloud session), `etc/` (`/etc/ucloud-sandboxes`), the chunk-index tokens, and `manifest.json` with every file's SHA-256 | 48 |
| `ucloud-sandbox-backup-chunk-index` (every 6 h) | The store node's index, copied online and `quick_check`ed there, gzip-compressed | 8 |

A snapshot is published by rename only once complete, and the two newest are
always kept. Run either part by hand with
`backup_gateway_state.py gateway|chunk-index --dest <dir> --keep <n>` as root
on the gateway; take a gateway backup before every upgrade.

Snapshots contain credentials. The directories are mode 0700, but every member
of the UCloud project can read the drive, so treat project membership as access
to production.

To verify a gateway snapshot, unpack it and check the digests in
`manifest.json`, run `PRAGMA quick_check` on each copied database, and run
`pg_restore --list postgres.dump`.

The relay's PostgreSQL also has a standalone dump tool,
`scripts/backup_relay_postgres.py`, for deployments that run PostgreSQL in a
container.

## Health watch and alerts

`ucloud-sandbox-ops-watch.timer` runs `ops_watch.py` every minute. It checks:

- the gateway, relay, placement, autoscaler, registry and PostgreSQL units;
- the gateway and relay `/healthz`, the registry `/v2/`, and `pg_isready`;
- gateway pressure as 60 s averages: CPU some (60%), memory full (5%), IO full
  (10%);
- root-disk free space (15%), and that the project drive is writable;
- store-node health, including replica verify failures, and the chunk index;
- backup age (gateway 2.5 h, chunk index 13 h);
- node init failures in the autoscaler journal over the last 15 minutes (3);
- the public gateway and relay links, when given.

The thresholds are `ops_watch.py` options. Results are in
`/var/lib/ucloud-sandboxes/ops-watch.json` (`failing` lists the failing
checks). A check alerts after two consecutive failures, every 6 h while it
stays failing, and once on recovery. Alerts go to the journal
(`journalctl -u ucloud-sandbox-ops-watch`). To also post them as
`{"text": …}` to a Slack-compatible webhook, set `ALERT_WEBHOOK_URL=` in
`/etc/ucloud-sandboxes/alerts.env` (mode 0600).

## Telemetry

Every service emits OpenTelemetry traces and metrics over OTLP/HTTP. A trace
backend (Tempo) owns sampled spans and a metrics backend (VictoriaMetrics) owns
time series. SQLite keeps only controller facts that affect scheduling and the
operator dashboard's state.

### Configuration

```json
{
  "telemetry": {
    "endpoint": "http://<gateway-host>:4318",
    "trace_sample_ratio": 0.1,
    "export_interval_ms": 5000,
    "export_timeout_ms": 3000,
    "max_queue_size": 4096,
    "max_export_batch_size": 512
  }
}
```

`endpoint` is an HTTP(S) origin; processes append `/v1/traces` and
`/v1/metrics`. An empty endpoint disables telemetry. Sampling is parent-based
at `trace_sample_ratio`. Workers receive the settings through VM init, so a
change reaches them at their next init.

### The gateway stack

```bash
sudo scripts/install_ucloud_observability_stack.sh \
  --private-bind-ip 0.0.0.0 --endpoint-host <gateway-host>
```

It runs four capped, low-priority containers: an OpenTelemetry Collector
(OTLP on port 4318), Tempo (72 h of traces), VictoriaMetrics (14 days of
metrics, leaving at least 20 GB of disk free) and Grafana. With `0.0.0.0` the
collector listens on every interface, which on a UCloud VM means the private
network; workers reach it at `--endpoint-host`. Grafana and the backend query
ports bind to loopback. Data lives under `--data-root` (default
`/var/lib/ucloud-sandboxes/telemetry`) on the gateway's local disk; never put
it on `/work`, where telemetry I/O would contend with images and snapshots.

This is a single-node diagnostic stack: losing the gateway loses retained
telemetry. If volume or the gateway's lifecycle outgrows it, move the stores to
external services behind the same collector endpoint.

### Reading it

**Report.** The installer also installs `ucloud-observability-report` on the
gateway: one bounded, secret-free JSON snapshot with service and backend
health, gateway memory, disk and load, telemetry container usage, operation
counts, errors and rates, p50/p95/p99 latency per service and operation, slow
trace summaries and their longest spans. Error traces are chosen before merely
slow ones. It reports both recent rate-based quantiles (`--rate-window`) and
cumulative quantiles since each process started, which matter for rare
operations such as park, wake, build and VM bootstrap. From a checkout with
the UCloud CLI logged in to the project:

```bash
scripts/read_ucloud_observability.sh <gateway-job-id> \
  --window 30m --rate-window 5m --trace-limit 5
```

It resolves the gateway's current SSH port and runs the report there. This is
the first diagnostic surface for agents; Grafana is for human exploration.

**Grafana.** Tunnel to the gateway
(`ssh -L 3000:127.0.0.1:3000 <gateway ssh command>`, where
`ucloud jobs ssh <gateway-job-id> --print-only` prints the command) and open
`http://127.0.0.1:3000/d/ucloud-platform-hot-paths`.

### What a trace contains

W3C `traceparent` and `tracestate` propagate across every synchronous
boundary: SDK to gateway, gateway to worker, worker to the privileged storage
service (whose Unix-socket protocol, schema 4, carries the context as a
field), and on to S3 or the registry. Relay requests store their trace headers
with the durable queue item, so worker delivery and a later wake correlate
after the caller has parked. Build and publication threads capture the context
before leaving the request thread.

Main span groups:

- `gateway.sandbox_create` with image resolution, placement, pull and node
  proxy phases;
- `gateway.node_response_headers`, `gateway.node_response_body` and
  `node.sandbox_exec_start`, separating fan-out, transfer and process start;
- `sandbox.park` and `sandbox.wake` with their checkpoint, storage, restore,
  readiness and commit phases;
- `storage.client.*` and `storage.server.*`, including semaphore queue wait;
- `snapshot.export_and_upload` and `snapshot.compact_and_upload`;
- `image.build.worker` with Docker build and push timings;
- `sandbox.exec.session`, the whole asynchronous process lifetime;
- the relay's HTTP spans and `relay.wait_for_worker`, with park and wake
  dispatch events;
- `autoscaler.reconcile`, provider calls and `vm.bootstrap`.

Every sampled HTTP response carries `X-Trace-Id`. IDs, generations, byte
counts, outcomes and phase timings are attributes; credentials, request bodies,
prompts, file contents, commands and S3 keys are never recorded. Sampled spans
of at least 1 ms carry `ucloud.span.thread_cpu.duration`; divided by the span's
wall time it separates waiting (low ratio) from CPU work (ratio near one). It
includes nested work on the same thread, and in asyncio services other
coroutines on the loop, so treat it as directional under load.

Metrics are `ucloud.platform.operation.duration` and
`ucloud.platform.operation.count`, labelled only by operation and outcome; there
are no per-sandbox series. Compare releases by `service.version` and
deployments by `deployment.id`; `cloud.provider` and `cloud.machine.type`
separate node profiles.

Worker heartbeats add snapshot-publication saturation under `actual_usage`
(`storage_publication_active`, `storage_publication_waiting`,
`storage_publication_limit`, plus cumulative counts, bytes and queue waits),
and immutable-environment I/O counters under `environment_io` (chunk-cache
hits, misses, downloaded bytes, prefetch). Publication saturation is
diagnostic only and does not drive autoscaling. The storage-native backend
serves its own Prometheus metrics on each worker at `127.0.0.1:9103`.

### Telemetry never slows product work

Spans go into one bounded in-memory queue drained by a daemon exporter thread.
Request threads only append; a full queue drops the span and counts it in
`dropped_spans`. Serialization, DNS, retries and network I/O never run on
request threads, and exporter failure never changes an API, park, wake, build
or provider result. `GET /v1/metrics` and the operator dashboard show exporter
queue capacity and accepted, exported, dropped and failed counts. Alert on
drops as well as latency: a fast graph over a saturated exporter is not
trustworthy.

### First views

Start from distributions, not averages: p50/p95/p99 park and wake by version;
park split into checkpoint, sealing, publication queue, export, upload and
commit; wake split into remote verification, restore and readiness; snapshot
upload throughput; storage semaphore and publication queue waits; build queue,
Docker build and push; provider create, bootstrap staging and remote init; and
relay model wait against response-to-wake latency.

## Snapshot store

`snapshot_store` selects where a park that must outlive its worker is
published: `registry` (the default) or `s3`. Local NVMe stays the working
store; an ordinary park stays on its worker, and only drain, detach or
scale-down requires publication. The gateway records the publication
descriptor and never proxies snapshot bytes. See
[state-protocol.md](state-protocol.md) for the publication transaction.

```json
{
  "snapshot_store": {
    "kind": "s3",
    "endpoint": "https://<s3-endpoint>",
    "bucket": "<bucket>",
    "region": "<region>",
    "prefix": "<prefix>",
    "access_key_id_env": "UCLOUD_SNAPSHOT_S3_ACCESS_KEY_ID",
    "secret_access_key_env": "UCLOUD_SNAPSHOT_S3_SECRET_ACCESS_KEY",
    "security_token_env": "UCLOUD_SNAPSHOT_S3_SECURITY_TOKEN"
  }
}
```

**Credentials.** The keys are named by environment variable and never written
to `deployment.json`. The autoscaler resolves them only while rendering a
worker's bootstrap; the worker installs a root-only credential file read by
the publisher and AgentEnv. Rotating a key means replacing that file or
reprovisioning the worker. `ucloud-sandbox-snapshot-gc.service` reads its keys
from `/etc/ucloud-sandboxes/snapshot-store.env`.

**Bucket setup.** Keep OCI content and snapshots under distinct prefixes even
in one bucket. Add a lifecycle rule that aborts incomplete multipart uploads
under `<prefix>/.uploads/` after 24 hours; abandoned uploads are not ordinary
objects, so GC cannot see them.

**Garbage collection.** `ucloud-sandboxes-snapshot-gc` is mark-and-sweep
over the gateway's routes, retained storage dependencies and unfinished
migrations. When an object's last reference disappears it writes a
first-seen-unreferenced marker under `<prefix>/.gc/unreferenced/`, and deletes
the object only after it has stayed unreferenced for the grace period; a
returning reference clears the marker. It refuses to delete while any route
lacks a dependency report, aborts on a missing or malformed protected manifest,
and stops at `--max-delete-objects`. It is a dry run without `--execute`. The
daily `ucloud-sandbox-snapshot-gc.timer` runs it with `--grace-days 7
--max-delete-objects 10000 --execute`, so an object survives seven to about
eight days after last use. `gateway-reconcile` enables the timer only when
`snapshot_store.kind` is `s3`. When upgrading, update workers and the gateway
before the timer runs again; collection stays blocked until every route has
dependency metadata.

**Publication limits.** Each worker publishes at most
`sandbox.storage_native_max_concurrent_publications` (default 4) snapshots at
once, narrower than the storage daemon's general limit so wake, mount and
delete keep slots. A wake that arrives before publication finishes gets a
retryable `snapshot_publication_pending` with `Retry-After: 1`.
`sandbox.storage_native_max_ublk_devices` bounds active ublk owners when
positive; the default 0 means no count ceiling. The warm device pool is sized
by `storage_native_pool_low_watermark` and `storage_native_pool_high_watermark`
(both 64). On kernels where `ublk_drv.ublks_max` counts every device (for
example Linux 6.8), it must cover the active devices plus the pool's high
watermark; newer kernels apply it only to unprivileged devices.

**Changing backends.** Changing either backend is its own rollout. The first
publication after switching snapshots to S3 compacts the old chain and the
local delta into one S3 layer, so a manifest never mixes origins; old
descriptors stay readable. To move the registry's blobs to S3, stop the
registry and run
`ucloud-sandboxes-registry-migrate --config <new config> --source-root <registry data root>`:
it hashes every source object, refuses unexpected target objects unless
`--allow-overwrite`, and verifies size and digest; it copies only with
`--execute`. Loading a configuration never moves bytes.

## Store node

The store node is a read-through cache (or, with `replica`, a full replica)
over the chunk-store S3 prefix on the private network. Workers range-read RAFS
images from it and never reach S3 themselves. With `serve_index` it also runs
`ucloud-chunk-index`. Design and configuration are in
[images.md](images.md).

- **Reads** go through `ucloud-chunk-serve` when `native_server_sha256` is
  set: it holds the `listen` address and answers resident reads on every core,
  and the Python node answers misses, errors and control on a loopback port
  behind it.
- **Health.** `GET /healthz` reports fill against budget, `verify_failures`
  and `full_refusals`; ops-watch alerts on it.
- **Metrics.** `GET /v1/metrics` with the index read token reports hits,
  misses, coalesced misses, bytes served, S3 requests, retries, hedges and
  hedge wins, S3 time-to-first-byte and fill percentiles, cache bytes,
  evictions, and extents that failed their check after a restart. A restart
  keeps the cache; extents are re-hashed on first use.
- **Warming before a burst.** Fill the node with a run's components:
  `ucloud-sandboxes warm-chunk-store --config /etc/ucloud-sandboxes/deployment.json --component sha256:… --wait`.
  Drivers can instead `POST /v1/warm` with the write token (a list of
  `{"key": "packs/ab/<sha256>.pack", "ranges": [[start, length], …] | null}`)
  and poll `GET /v1/warm/<job>`.
- **Removing a bad object.** `DELETE /v1/objects/<key>` with the write token
  removes an object's cached extents when an operator found them bad.
- **Never open the live cache as root**, for example by constructing an
  `ExtentCache` in a diagnostic. It recreates `cache/tmp` owned by root, and
  every later fill fails with `PermissionError` until the directory is given
  back to `ucloud`.
- **Failure is closed.** Losing the node stops RAFS reads (workers see EIO);
  it never serves wrong bytes. S3 still holds everything, so a replacement
  starts cold or from the index backup (below).

A worker initialized before the chunk store was configured has no store-node
URL, so its RAFS reads fail closed; new workers get it from node init. Drain
such workers or let idle scale-down replace them.

### Released images

After migration to the chunk store, `chunk-migrate release-oci` deletes the OCI
manifests of switched images that are not build inputs (dry run without
`--execute`; with `--include-build-inputs` and verification receipts it also
releases build inputs). It remembers each image's tags in `image-roots.sqlite3`
first.

- **Readers.** The registry answers first. On a 404 the image's `released`
  row answers instead (a tag through its remembered digest), for image
  resolution, the dependency resolver and the hourly prune. Workers attach the
  root the gateway dispatched, never the manifest annotation.
- **Builds on a released base.** With `immutable_environments.regenerate_bases`
  on, a build naming a released image whose regenerated copy is missing gets
  `503 base_regenerating` (`Retry-After: 30`) while the gateway runs
  `chunk-migrate regenerate` for it; the retry builds from
  `ucloud-regenerated:…`. A failed regeneration answers `409 base_released`
  for 10 minutes before retrying. Copies are leased per build and dropped by
  the hourly prune 30 days after their last use. Regenerate ahead of an eval
  with `chunk-migrate regenerate --config … --image repo@sha256:…`.
- **Refused.** A commit build on a released parent fails with
  `commit_parent_released`; prepare scripts that read the OCI of a released
  image fail; `chunk-migrate inventory` no longer lists them.
- **Rollback.** Regenerate the OCI from its root with
  `ucloud-sandboxes unpack-environment --config … --chunk-index-token-file … --work-root … --root <root> --output-ref <ref>`
  and re-push the remembered tags. The tree is reproduced exactly, but the
  manifest, config and layer digests are new.

## Releases and rollback

The upgrade procedure is in [deployment.md](deployment.md#upgrades). In
production:

- Stage each release kit (wheel, both node bundles, the configuration) in its
  own directory, and keep the live kit on the project drive so a replacement
  gateway can be installed from it.
- Upgrade while the fleet is idle and no builds are queued; take a gateway
  backup first.
- Never wrap a production restart or configuration publish in a retry loop.
  If a step fails, stop and inspect.
- Rollback reinstalls the previous wheel and configuration and reconciles.
  Workers take a new bundle only at their next init; drain or wait for
  scale-down to replace running ones.

## When something is lost

**A worker or builder.** The autoscaler replaces it. Parks published to the
snapshot store wake elsewhere; running sandboxes on a lost worker are lost and
their requests return `410 node_lost` (see
[state-protocol.md](state-protocol.md)).

**The store node.** RAFS reads stop until it returns. To replace it:

1. Create a VM like the current one with `submit-vm --role node --hostname <store-host> --no-public-link --product-id <product> --disk-gb <size> --label ucloud-sandboxes/store=true`.
   In the payload, remove `timeAllocation` and the `ucloud-sandboxes/node`
   label (the autoscaler must not own the VM) before submitting with
   `--execute`.
2. Restore the newest chunk-index backup to
   `/var/lib/ucloud-chunk-index/index.sqlite` on the new VM (owner `ucloud`,
   mode 0600): from the gateway, `gunzip -c` the snapshot into
   `ssh -i <data_root>/ssh/gateway-init ucloud@<store-host>`, then run
   `PRAGMA quick_check`.
3. From the gateway, run store init with the live release's sandbox bundle and
   the chunk-store S3 key in the environment:
   `ucloud-sandboxes init-vm <job-id> --config /etc/ucloud-sandboxes/deployment.json --role store --package-spec <release>/sandbox-node-package.tar.gz --ssh-private-key-file <data_root>/ssh/gateway-init --execute`.
4. Check `/healthz` and `/v1/metrics` as above.

The replica refills from S3 on its own (about two hours for a few hundred GB).
Keep the hostname: the configuration names it, so a new address needs no
change.

**The gateway.**

1. Create a VM like the gateway's: private network, the project drive mounted,
   both public links.
2. `apt-get install ca-certificates curl docker.io nftables openssl python3-venv postgresql-18`.
3. From the newest gateway backup, stage the installer's inputs in the stage
   directory (`$STAGE`, mode 0700):
   - `state.tar` made from `state/` (`tar -cf state.tar state`);
   - `ucloud.pgdump` from `postgres.dump`;
   - `deployment.json`, `postgres.dsn` and `s3.env` from `etc/`;
   - `ucloud-session.json` from `state/`;
   - `release/` with the live kit's two node bundles;
   - the live kit's wheel.
4. Run `scripts/install_ucloud_gateway.sh` as root. It disables unattended
   upgrades, restores PostgreSQL (refusing an existing `ucloud` database),
   unpacks the state into `/var/lib/ucloud-sandboxes/state` (refusing an
   existing one), installs the venv, units and drop-ins, and stops before
   starting services. Put the chunk-index tokens from the backup in
   `/var/lib/ucloud-chunk-index/`.
5. Keep the gateway's hostname; then nothing else changes. With another
   hostname:
   - run `scripts/rehome_registry_host.py --config /etc/ucloud-sandboxes/deployment.json --from <old-host>:5000 --to <new-host>:5000`
     as the service user, review the counts, then add `--execute`. It rewrites
     the registry authority in every stored image reference, refuses while
     gateway services run, and uses one transaction per database;
   - update `gateway_private_host`, `builder.buildx_cache_ref`,
     `sandbox.network_relays` and `telemetry.endpoint`;
   - clients' `guest_relay_url` names the relay's host, so it changes too.
6. `python -m ucloud_sandboxes.systemd gateway-reconcile --config /etc/ucloud-sandboxes/deployment.json`.
7. Reinstall `install_ucloud_ops.sh` and the observability stack.

At most the last hour of gateway state is lost. Sandbox routes come back from
PostgreSQL, and workers re-register with their next heartbeat.
