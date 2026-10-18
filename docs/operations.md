# Operations

How to deploy, upgrade, observe and recover the service. The design is in
[architecture.md](architecture.md), images and the chunk store in
[images.md](images.md), the HTTP surface in [api.md](api.md).

UCloud is the primary provider and the only one with in-tree deployment
automation; Hetzner Cloud is a supported alternative.

## Machines and their state

| Machine | Created by | Runs | Durable state |
| --- | --- | --- | --- |
| Gateway VM | Operator (`submit-vm --role gateway`) | Gateway API, placement worker, model relay, private registry and its timers, autoscaler, build service, PostgreSQL 18, observability stack | `data_root`, PostgreSQL, `/etc/ucloud-sandboxes` |
| Store node | Operator, then `init-vm --role store` | Chunk store (read cache or replica over S3), optionally chunk index and `ucloud-chunk-serve` | Chunk-index database and replica cache; S3 holds the permanent copy |
| Sandbox workers | Autoscaler | gVisor Warden (`serve-direct-node-agent`), storage-native service, optional `noded` | None |
| Builders | Autoscaler | `serve-builder-agent`, Docker, Buildx | None |

Registry blobs, backups and release kits live on the project drive
(`/work/data`). In production `data_root` and PostgreSQL sit on the gateway's
local disk (`/var/lib/ucloud-sandboxes/state`), backed up hourly to the
project drive; local disks end with their jobs. Gateway units
(`ucloud-sandbox-*`) run as the `ucloud` user.

**Use DNS names, never private IPs.** UCloud changes a VM's private address
when it restarts the VM. Name the gateway and store node by hostname in
`gateway_private_host`, `builder.buildx_cache_ref`, `sandbox.network_relays`,
`telemetry.endpoint` and `chunk_store.store_node.url`; clients name the relay
host in `guest_relay_url`.

## Configuration

`deployment.json` is one exact JSON object with `"schema": 5`; unknown or
missing keys and older schemas fail loading. Start from:

```bash
uv run ucloud-sandboxes sample-config > deployment.json
```

All keys and defaults are in `ucloud_sandboxes/config.py`. The ones an
operator must set:

| Key | Set to |
| --- | --- |
| `deployment_id` | Your deployment's identity; jobs and heartbeats must match it |
| `provider` | `{"kind": "ucloud", "scope_id": <project>, "private_network_id", "gateway_public_link_id", ...}` |
| `data_root` | Gateway state directory |
| `node_package_root` | Release directory holding both node bundles |
| `gateway_private_host` | The gateway's DNS name |
| `relay_postgres` | `{dsn_file, schema, ...}`; the relay refuses to start without it |
| `registry_disk_*_percent` | All three `100` on UCloud's shared `/work/data`; bound the registry with `registry_retention_days` and `registry_keep_per_repository` instead |
| `telemetry.endpoint` | `http://<gateway-host>:4318`, or empty to disable |
| `sandbox`, `builder` | Products, disk sizes, `max_nodes`, [network relays](#sandbox-network-policy) |
| `immutable_environments` | Chunk store and store node; see [images.md](images.md) |

Credentials never appear in the file: S3 keys are named by environment
variable, and the UCloud session is a separate file. Apply changes with
`gateway-reconcile`; workers pick up node settings at their next VM init.

## First deployment on UCloud

1. **Build the release** (see [Releases](#releases)): the wheel,
   `ucloud-direct-runsc`, `managed-init` and the storage-native manifest.
2. **Create the gateway VM.** Review the payload, then add `--execute`:

   ```bash
   uv run ucloud-sandboxes submit-vm --config deployment.json \
     --role gateway --mount <project-drive-path> --hostname <gateway-host> \
     --output json
   ```

   It joins the private network, mounts the project drive at `/work/data` and
   binds public links to the gateway and relay ports. Remove `timeAllocation`
   from the payload for a long-lived VM.
3. **Install PostgreSQL** on the gateway:

   ```bash
   apt-get install postgresql-18
   sudo -u postgres createuser --login ucloud
   sudo -u postgres createdb -O ucloud ucloud
   echo 'host=/var/run/postgresql dbname=ucloud user=ucloud' > <dsn-file>   # mode 0600, owner ucloud
   python -m ucloud_sandboxes.shared_control migrate --dsn-file <dsn-file> --deployment-id <id>
   ```

   Name `<dsn-file>` in `relay_postgres.dsn_file`. To keep routing in PostgreSQL
   too (production does), run
   `python -m ucloud_sandboxes.shared_control.routing_cutover --routing-file <data_root>/routes.sqlite --dsn-file <dsn-file> --schema <schema>`
   once, with the gateway, relay and autoscaler stopped.
4. **Converge the gateway.** Inspect the script, then rerun with `--execute`:

   ```bash
   uv run ucloud-sandboxes deploy-all-in-one <gateway-job-id> \
     --config deployment.json \
     --wheel dist/ucloud_sandboxes-<version>-py3-none-any.whl \
     --direct-runsc <path>/ucloud-direct-runsc \
     --managed-init <path>/managed-init \
     --storage-native-manifest <path>/storage-native-manifest.json \
     --output script
   ```

   It builds both node bundles, installs the wheel into
   `/work/ucloud-sandboxes/gateway-venv`, creates missing credentials and the
   SSH bootstrap key (registering it with UCloud), installs config and units,
   and runs `gateway-reconcile`. Rerunning it keeps existing credentials.
5. **Install ops tooling** on the gateway:

   ```bash
   sudo scripts/install_ucloud_ops.sh --backups <dir under /work/data> \
     --public-gateway <gateway-url> --public-relay <relay-url>
   sudo scripts/install_ucloud_observability_stack.sh \
     --private-bind-ip 0.0.0.0 --endpoint-host <gateway-host>
   ```

6. **Create the store node**, if the chunk store uses one: steps 1, 3 and 4
   of [Store node lost](#store-node-lost), without the restore.
7. **Verify:**

   ```bash
   curl -fsS https://<gateway-url>/healthz        # reports the package version
   curl -fsS https://<relay-url>/healthz
   curl -i   https://<gateway-url>/v1/sandboxes   # 401 without a key
   curl -fsS -H "Authorization: Bearer $(cat <data_root>/sandbox-api-token)" \
     https://<gateway-url>/v1/sandboxes
   ```

   Then check the units, the autoscaler journal (`autoscaler --once --output json`
   previews a cycle), and one image build and sandbox create.

### Service convergence

```bash
sudo python -m ucloud_sandboxes.systemd gateway-reconcile \
  --config /etc/ucloud-sandboxes/deployment.json
```

The one convergence routine for every provider: enables timers and optional
services by configuration, restarts everything in order and waits for health.
Run it after any change to the wheel, units or `deployment.json`. Hosts never
upgrade OS packages unattended; package updates are a deliberate task.

## Releases

A release is the wheel plus two digest-pinned node bundles
(`sandbox-node-package.tar.gz`, `builder-node-package.tar.gz`, each with a
`.sha256`). Nodes never contact a package repository.

```bash
uv build                                   # dist/ucloud_sandboxes-<version>-py3-none-any.whl
python scripts/verify_installed_wheel.py   # in a venv with the wheel installed
```

Native components (`runsc`, managed init, storage-native, `noded`,
`chunk_serve`, `nydusd`) each have a pinned build script under `runtime/`.
`deploy-all-in-one` builds bundles from scratch; when OS packages and kernel
are unchanged, repack instead:

```bash
python scripts/repack_node_bundle.py --source <old sandbox bundle> --wheel <wheel> \
  --storage-backend ... --storage-manifest ... --storage-license ... --output <new sandbox bundle>
python scripts/derive_builder_bundle.py --source <new sandbox bundle> \
  --buildx-deb <deb> --output <new builder bundle>
```

Its `--help` covers replacing `runsc`, the kernel module closure or Python
dependencies. A bundle's modules match exactly one kernel; replace the closure
whenever the kernel changes.

**Every release repacks both bundles.** Placement only sends work to nodes
whose agent version equals the gateway's, so a gateway upgrade without new
bundles leaves the fleet unschedulable.

### Upgrading

Upgrade while the fleet is idle and no builds are queued. Never wrap a
production restart or config publish in a retry loop; if a step fails, stop
and inspect.

1. Stage the wheel, both bundles and the config in a new release directory on
   the project drive.
2. Back up: `sudo /usr/local/libexec/ucloud-sandboxes/backup_gateway_state.py gateway --dest <backups> --keep 48`.
3. `sudo systemctl stop ucloud-sandbox-registry-prune.timer`.
4. Install the wheel and any changed units:

   ```bash
   /work/ucloud-sandboxes/gateway-venv/bin/python -m pip install --force-reinstall '<wheel>[postgres]'
   ```

   Copy changed unit files from the package's `systemd/` directory to
   `/etc/systemd/system/`.
5. Point `node_package_root` at the new release directory and apply any config
   change `CHANGELOG.md` calls for. Apply new PostgreSQL tables before
   restarting (`python -m ucloud_sandboxes.shared_control migrate ...`).
6. Run `gateway-reconcile`, confirm `/healthz` reports the new version, and
   `systemctl start ucloud-sandbox-registry-prune.timer`.
7. Workers and builders take the new bundle at their next VM init. Old nodes
   keep their sandboxes but get no new placements; drain them or let idle
   scale-down replace them.

**Rollback:** reinstall the previous wheel and config and reconcile; restore
the pre-upgrade backup if the release changed database contents. Do not roll
back only the gateway while newer workers are still admitted.

### Replaying VM init

The autoscaler initializes workers and builders. To replay init on one VM:

```bash
ucloud-sandboxes init-vm <job-id> --config /etc/ucloud-sandboxes/deployment.json \
  --role sandbox|builder|store --package-spec <release>/sandbox-node-package.tar.gz \
  --ssh-private-key-file <data_root>/ssh/gateway-init [--execute]
```

On failure, read the `UCLOUD_INIT_PHASE` lines and the `ucloud-sandbox-node`,
`ucloud-storage-native` and Docker journals, fix the bundle or config, and
replay. Never repair a node by hand-installing a different artifact.

## Backups

`install_ucloud_ops.sh` installs two timers (it refuses a backup directory
outside `/work`):

| Timer | Contents | Kept |
| --- | --- | --- |
| `ucloud-sandbox-backup-gateway` (hourly) | `postgres.dump`, `state/` (online SQLite copies, tokens, SSH and producer keys, UCloud session), `etc/`, chunk-index tokens, `manifest.json` with SHA-256s | 48 |
| `ucloud-sandbox-backup-chunk-index` (6 h) | The store node's index, `quick_check`ed and gzipped | 8 |

Run either by hand: `backup_gateway_state.py gateway|chunk-index --dest <dir> --keep <n>`
as root on the gateway. To verify a snapshot: check the digests in
`manifest.json`, run `PRAGMA quick_check` on each database, and
`pg_restore --list postgres.dump`.

Backups contain credentials. Every member of the UCloud project can read the
project drive, so project membership is access to production.

## Observability

**Health watch.** `ucloud-sandbox-ops-watch.timer` runs `ops_watch.py` every
minute: unit states, gateway/relay `/healthz`, registry `/v2/`, `pg_isready`,
gateway CPU/memory/IO pressure, disk space, store-node health, backup age,
recent node-init failures and the public links. Current results are in
`/var/lib/ucloud-sandboxes/ops-watch.json` (`failing` lists failing checks).
A check alerts after two consecutive failures, every 6 h while failing, and on
recovery. Alerts go to `journalctl -u ucloud-sandbox-ops-watch`; set
`ALERT_WEBHOOK_URL=` in `/etc/ucloud-sandboxes/alerts.env` (mode 0600) to post
them to a Slack-compatible webhook. Thresholds are `ops_watch.py` options.

**Telemetry.** `install_ucloud_observability_stack.sh` runs an OTLP
collector (port 4318), Tempo (72 h), VictoriaMetrics (14 days) and Grafana on
the gateway's local disk (never on `/work`); losing the gateway loses it.
The first stop is the secret-free JSON report (health, latency percentiles,
errors, slow traces), from a checkout with the UCloud CLI logged in:

```bash
scripts/read_ucloud_observability.sh <gateway-job-id> --window 30m --rate-window 5m --trace-limit 5
```

For Grafana, tunnel `-L 3000:127.0.0.1:3000` over the SSH command that
`ucloud jobs ssh <gateway-job-id> --print-only` prints and open
`http://127.0.0.1:3000/d/ucloud-platform-hot-paths`. Sampled responses carry
`X-Trace-Id`; `GET /v1/metrics` (gateway token) shows exporter drops.

## Store node

The store node serves RAFS image reads to workers from the chunk-store S3
prefix; workers never reach S3 themselves. Design and config are in
[images.md](images.md).

- **Health:** `GET /healthz` (ops-watch alerts on it); `GET /v1/metrics`
  with the index read token.
- **Warm before a burst:**
  `ucloud-sandboxes warm-chunk-store --config /etc/ucloud-sandboxes/deployment.json --component sha256:... --wait`.
- **Remove a bad object:** `DELETE /v1/objects/<key>` with the write token.
- **Never open the live cache as root** (for example constructing an
  `ExtentCache` in a diagnostic): it recreates `cache/tmp` owned by root and
  every later fill fails until it is chowned back to `ucloud`.
- **Fails closed:** without the node, RAFS reads return EIO; wrong bytes are
  never served. Workers initialized before the store node was configured have
  no URL for it; drain them or let scale-down replace them.

## Recovery

**Worker or builder lost.** The autoscaler replaces it. Parks published to the
snapshot store wake elsewhere; sandboxes running on it are lost and return
`410 node_lost`.

### Store node lost

RAFS reads stop until it returns. Keep the hostname so no config changes.

1. Create the VM; then, in the payload, remove `timeAllocation` and the
   `ucloud-sandboxes/node` label (the autoscaler must not own it) and submit
   with `--execute`:

   ```bash
   uv run ucloud-sandboxes submit-vm --config /etc/ucloud-sandboxes/deployment.json \
     --role node --hostname <store-host> --no-public-link --product-id <product> \
     --disk-gb <size> --label ucloud-sandboxes/store=true --output json
   ```

2. Restore the newest chunk-index backup to
   `/var/lib/ucloud-chunk-index/index.sqlite` (owner `ucloud`, mode 0600):
   `gunzip -c <backup> | ssh -i <data_root>/ssh/gateway-init ucloud@<store-host> 'cat > /var/lib/ucloud-chunk-index/index.sqlite'`,
   then `PRAGMA quick_check`.
3. With the chunk-store S3 key in the environment, run `init-vm ... --role store`
   (above) with the live release's sandbox bundle and `--execute`.
4. Check `/healthz` and `/v1/metrics`. A replica refills from S3 on its own
   (about two hours for a few hundred GB).

### Gateway lost

At most the last hour of state is lost; routes come back from PostgreSQL and
workers re-register on their next heartbeat.

1. Create a VM like the old gateway: private network, project drive, both
   public links, the same hostname if possible.
2. `apt-get install ca-certificates curl docker.io nftables openssl python3-venv postgresql-18`
3. Stage the installer's inputs from the newest backup in `$STAGE` (mode 0700):
   `state.tar` (`tar -cf state.tar state`), `ucloud.pgdump` (from
   `postgres.dump`), `deployment.json`, `postgres.dsn` and `s3.env` (from
   `etc/`), `ucloud-session.json` (from `state/`), `release/` with the live
   kit's two bundles, and the live kit's wheel.
4. `sudo scripts/install_ucloud_gateway.sh`. It restores PostgreSQL and the
   state directory (refusing existing ones), installs venv, units and
   drop-ins, and stops before starting services. Copy the chunk-index tokens
   from the backup to `/var/lib/ucloud-chunk-index/`.
5. **Only if the hostname changed**, rehome the registry. As the service user,
   with gateway services stopped (review the counts, then add `--execute`):

   ```bash
   python scripts/rehome_registry_host.py --config /etc/ucloud-sandboxes/deployment.json \
     --from <old-host>:5000 --to <new-host>:5000
   ```

   It rewrites the registry host in every stored image reference. Then update
   the DNS-named keys listed under [Machines](#machines-and-their-state);
   clients must change `guest_relay_url`.
6. Run `gateway-reconcile`.
7. Rerun `install_ucloud_ops.sh` and `install_ucloud_observability_stack.sh`.

## Credentials

Random tokens in `data_root`, one per channel; never reuse one elsewhere.

| File | Holder | Grants |
| --- | --- | --- |
| `sandbox-api-token` | SDK clients | Public sandbox and image routes only |
| `gateway-token` | Operators, autoscaler, relay | Every gateway route; stays on the gateway |
| `heartbeat-token` | Workers | Heartbeat publication only |
| `node-control-token` | Gateway | Every node route except `/healthz` |
| `relay-sandbox-token`, `relay-worker-token` | Sandboxes, inference workers | The model relay |
| `ssh/gateway-init` | Autoscaler | SSH bootstrap of new VMs |

Give clients the public gateway URL and `sandbox-api-token` (inference
workers also the relay URL and `relay-worker-token`); never the gateway token.
Tokens rotate independently. Nodes read their tokens at agent start, so rotate
the heartbeat token on gateway and nodes together and replay init. Rotating
the gateway token invalidates open exec session URLs. Snapshot-store S3 keys
reach workers only at init. Never expose port 8090, the registry port or the
relay's private port publicly; public traffic arrives over TLS through UCloud
public links (or Nginx on Hetzner).

## Sandbox network policy

Sandboxes on deployed workers get their own network namespace with NAT and
cannot reach the host or any private range (`10/8`, `100.64/10`, `172.16/12`,
`192.168/16`, link-local, loopback). Two settings open exact holes:

- `sandbox.network_relays`: `{"<name>": "<host>:<port>"}`, e.g.
  `{"default": "<gateway-host>:8092"}` for the model relay. Sandboxes reach a
  relay by hostname via a virtual address the worker translates, so use DNS
  names. A sandbox with `network_policy: {"egress": "relay", "relay": "default"}`
  reaches only that endpoint. Relays must be on another host than the worker;
  add a new name rather than changing a name's host or port.
- `sandbox.direct_network_allow_tcp`: exact `IPV4:PORT` services every
  direct-egress sandbox may reach. Prefer a relay for anything reached by
  name.

Placement requires a worker to advertise the relay, so changes take effect
as workers are re-initialized.

## Hetzner Cloud

The `hetzner` provider autoscales private-network workers and builders.
Required keys: `scope_id`, `network_id`, `sandbox_image`, `builder_image`
(snapshot IDs), `ssh_key_ids`; the API token is read from `api_token_env`
(default `HETZNER_API_KEY`). Private-only workers need
`enable_private_egress: true`, with the gateway NATing their traffic.

1. `scripts/setup_hetzner_foundation.py --egress-gateway-ip <ip> --execute`
   (network, SSH key, firewalls).
2. Prepare PostgreSQL as in UCloud step 3.
3. Stage the wheel, `deployment.json`, both bundles, the SSH key pair, the
   Hetzner env file and `configure_hetzner_sdk_ingress.sh` in `/tmp`; run
   `scripts/install_hetzner_gateway.sh --public-ip <ipv4>`.
4. `scripts/configure_hetzner_sdk_ingress.sh --public-host <dns-or-ip>` (Nginx
   and Let's Encrypt; relay under `/relay/`).

Boot workers from a snapshot so the kernel matches the bundle: on a drained,
disposable source server run
`scripts/prepare_hetzner_snapshot.sh "$(hostname)" <docker-image-gib> sandbox|builder`,
power off, snapshot, and canary one clone (create, exec, park, wake).

## Adding a provider

Implement `ucloud_sandboxes.providers.base.ComputeProvider` (list, create,
terminate and SSH bootstrap for instances, with normalized lifecycle states
and labels preserved for recovery) and register a factory under the
`ucloud_sandboxes.compute_providers` entry-point group, keyed by the config
`kind`. Policy, routing and runtime stay unchanged. The target cloud still
needs privileged Linux VMs with the bundle's kernel modules (`ublk_drv`,
`erofs`, `nbd`, `nft_log`, `nfnetlink_log`, `virtiofs`), private reachability
from the gateway, durable gateway storage with PostgreSQL 18, and its own
gateway installer. Object storage can be any S3-compatible endpoint.
