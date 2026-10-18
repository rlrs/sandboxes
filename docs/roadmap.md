# Roadmap

Open work only, as of release 0.9.75 (2026-10-18). Finished work is
described in the reference docs: [architecture](architecture.md),
[node admission and memory](node-admission-and-memory.md),
[images](images.md), [model relay](model-relay.md),
[scaling policy](scaling-policy.md) and [decisions](decisions.md).

Each item says what is left, why it matters, and what it touches. Items that
need a call rather than work are under [Open decisions](#open-decisions).

## Node daemon (`ucloud-noded`)

- **Files through the daemon.** Single-file writes and archive uploads still
  go through the Python agent; noded only proxies them. Per-script upload is
  the largest setup cost left at a 512-rollout burst. Touches the file routes
  in `node_agent.py` and noded's request front (`runtime/noded/src/exec_front.rs`).
- **Park, wake, migration, commit and reconcile in noded, then delete the
  Python agent.** These are the last per-sandbox paths on the agent's GIL, and
  they hold most of the node's crash and fence invariants. Touches
  `direct_service.py`, `direct_warden.py`, `hibernation.py`, `node_agent.py`.
- **Heartbeats from noded.** The Python agent still composes and sends them
  (`node_agent.py`, `heartbeat_sender.py`), so every heartbeat costs agent GIL
  time.
- **Create-path tails.** `runsc_create`, storage prepare and registry commit
  set the p95 of a node-side create. Storage prepare still runs `mkfs.xfs` per
  create; a pre-made blank filesystem would remove it. Touches
  `storage_native_daemon.py`, `runtime/noded/src/storage.rs`,
  `runtime/noded/src/create.rs`, `runtime/noded/src/registry/`.
- **CPU QoS for background work.** No `cpu.idle` or `cpu.weight` class exists
  for prefetch, publication, offload and cache fill, so they compete with
  foreground tool calls.
- **IPv6 on agent-made veths.** noded's own pairs have IPv6 off; the pairs the
  Python agent still makes for relay egress (`direct_network.py`) do not.

## Admission

- **The last two gates for Rust admission.** A drain in the middle of a burst
  must not report ready before its creates finish, and the growth-admission
  span at the density shape (about 180 sandboxes per node) must stay well
  under 1 s. These guard data loss on retire and the 20–30 s response holds
  seen before. Run with `sandbox.direct_node_rust_admission` on, using
  `scripts/bench_rl_scale.py` and `scripts/live_relay_load_benchmark.py`.
- **Delete the Python admission ledger** one release after the flag is on:
  `transition_admission.py`, the ledger, pool and growth code in
  `direct_service.py`, `DemandPublisher` in `pause_handoff.py`, the agent's
  `growth/events` handler and `agent-demand.json`. Two ledgers are a
  split-brain risk. `admission.py` stays; the gateway uses it.
- **Gateway wake admission.** `wake_admission.py` and `resource_admission.py`
  still reserve capacity for wakes at the gateway, beside the node's own
  admission. One decision point is simpler and cannot disagree with itself.

## Writable state and memory

- **Hibernate cost independent of bytes written** (gVisor patch 0008, an
  external filestore backing). Today a checkpoint serializes the guest's whole
  filestore, so a park after large writes takes tens of seconds. Touches
  `runtime/gvisor/`, `build_pinned.sh` and the capture reservation in
  `direct_warden.py`.
- **One writable backing.** Replace the per-sandbox ublk device, `mkfs.xfs`
  and online growth with node-local XFS project quotas and reflink. It removes
  the 0.5 s growth poller that let large in-sandbox unpacks hit `ENOSPC`, and
  the `storage_native_*.py` modules with their AgentENV writable patches
  (`runtime/storage_native/`).

## RL state primitives

- **Commit, gateway half.** The worker export
  (`POST /v1/sandboxes/{id}/commit-export`, `node_commit.py`) and the
  builder's `publish_commit` exist but nothing calls them. Missing: the
  gateway route, the `image_commits` table, the reconciler and the SDK call;
  `commit_steps.py` is the wiring point. Commit is how a prepared rollout
  state becomes a reusable image. Commits on a parent whose OCI was released
  are refused (`commit_parent_released`); they could build from a regenerated
  copy instead.
- **Fork and templates.** Not started. Starting N children from one
  checkpoint would skip per-rollout setup. Open questions: partial forks
  (`min_count < count`) and whether commits can cross deployments.
- **Node-side hydration.** Image recipes and `ensure` build images ahead at
  the gateway, but nothing warms a worker's cache for a planned group before
  its creates land. Touches `environment_backend.py` and a new node route.

## Images and chunk store

- **Python off the image data path.** nydusd serves RAFS images, but plain
  EROFS components still go through the Python NBD export: toolkits, and any
  image M2 did not convert. Move toolkits to a nydusd-served format (or
  convert the stragglers), then delete `environment_nbd.py`,
  `environment_cache.py` and the Python RAFS reader in `environment_rafs.py`.
- **Docker off sandbox workers.** Workers still install Docker and require
  `docker.service`; `DockerOverlay2RootfsStore` (`image_rootfs.py`) and
  `image_eviction.py` remain. Removing them shrinks worker boot and the code
  that only serves pre-EROFS rootfs.
- **No per-create init copy.** `/.ucloud-init` and `/.ucloud-job-init` are
  copied into every rootfs (`install_init`, `install_managed_init` in
  `runtime/noded/src/guest.rs`); a read-only mount would avoid the write.
- **Placement by chunk residency.** Heartbeats do not say which nodes already
  hold an image's chunks, so placement cannot prefer them. Touches
  `gateway/heartbeats.py` and the chooser in `gateway/placement.py`.
- **S3 outage windows.** The chunk store's durable tier is S3 at another
  provider. Stalls are bounded since 0.9.67, but outages still fail commits
  and fills until it recovers.

## Scaling and control plane

- **Cluster create rate.** The M2 target is at least 300 creates/s; about
  30/s is measured on production's worker cap. Bound by per-node create
  throughput (above) and by the control plane (below).
- **Cold start.** From zero, ready time is dominated by VM provisioning
  (ready p50 about 2 minutes). There is no image-gated scale-up: a new worker
  opens admission before it has fetched the queued groups' metadata and
  traces. Touches `policy.py` and readiness in `node_agent.py`.
- **Finish power-of-k placement.** The code default is still `ranked`. Flip
  `gateway_create_placement` to `power_of_k` and delete the ranked create path
  (its block in `control_plane.py`; `select`, `rank`, `select_and_reserve`
  and `alternate_available` in `gateway/placement.py`;
  `allocate_sandbox_create_with_pending`; `RegistryLayerMetadataCache`).
  Then move wakes to node-gated admission and delete
  `worker_capacity_revisions`, the advisory turns, the scheduling locks and
  the placement service.
- **Exec routes without the database.** Current workers (agent and noded)
  name exec sessions under the signed prefix, so the fallback `upsert_exec` in
  `control_plane.py` and the `exec_sessions` table can go once no older
  worker can join.
  Events and files still read the route table per request; a route token on
  each request would make ingress stateless.
- **One gateway host.** API, PostgreSQL, the relay and the registry share one
  VM, so a gateway outage stops everything. Splitting it needs host-local
  state moved to PostgreSQL first: heartbeats and quarantine in
  `control-state.sqlite`, `host_locks.py` users, build history and
  autoscaler state.
- **Node loss.** The failure rules are implemented, but the observe-only
  experiment that would set the suspension and return timers has not run.
  Known gaps: an occupied worker that never returns waits for an operator;
  nothing alerts on an `unavailable` node; an untimed suspension is
  re-quarantined every cycle; a reap failing longer than the 7-day loss
  retention is forgotten.

## Relay

- **Packet watcher cost at density.** Node-local waits read one NFLOG
  message per relay packet; the cost at 500 sandboxes per node is unmeasured.
- **Local waits need `guest_relay_url`.** A trainer that calls the relay
  through the public ingress still gets relay-driven park and wake; see the
  decision below.

## Open decisions

- **Turn on `sandbox.direct_node_rust_admission`.** Built and gated on
  production workers except the two gates above; off by default and in
  production. Turning it on unlocks the admission deletions.
- **Make noded the default and delete the Python duplicates.** The code
  defaults are off for `sandbox.direct_node_front_door`,
  `direct_node_rust_create`, `direct_node_rust_exec`,
  `direct_node_rust_pause`, `direct_pause_tier` and
  `direct_local_model_waits` (`ucloud_sandboxes/config.py`), although
  production runs with them on. Flipping the defaults lets the Python create,
  exec and pause paths (`direct_provisioner.py`, `sandbox_exec.py`,
  `pause_tier.py`, `local_wait.py`, `warm_park.py`, `resident_memory.py`) be
  deleted, once nothing noded forwards still lands in them.
- **Wire or remove the guest agent.** `ucloud-sandbox-init agent`
  (`runtime/managed_process/agent_linux.go`) and its Python listener
  `ucloud_sandboxes/guest_agent.py` were built but never wired into the exec
  path; only `tests/test_guest_agent.py`, `tests/test_archive_upload.py` and
  `scripts/benchmark_guest_agent.py` use them. `runsc exec` per command
  already meets the per-node exec target. Wiring it would cut about 26 ms of
  CPU per exec; removing it deletes about 800 lines of Python plus the Go
  subcommand.
- **Retire relay-driven park and wake.** Possible once every trainer sets
  `guest_relay_url`; until then the public-ingress path keeps them.
- **Delete the legacy SQLite relay importer** (`shared_control/legacy_relay.py`,
  `import_idle_relay` in `shared_control/migration.py`).
- **The registry disk guard on a shared filesystem.** `registry_disk.py`
  measures the registry's filesystem with `statvfs`. On a shared project
  drive that number is other tenants' usage, so the 70/60/90 % thresholds can
  evict images or refuse builds for reasons unrelated to the registry. Replace
  it with a byte bound or turn it off there.
- **One memory mode.** Three backings remain (`direct_split_memory_backing`,
  `direct_ram_memory_backing`, `direct_reflink_memory_restore`;
  `memory_backing.py`, gVisor patches 0006 and 0007). Pick one and delete the
  rest.
- **A second store node.** One store node fronts S3. When it was lost once,
  its cache took about 1.5 hours to refill and first commands waited on S3
  meanwhile. A second node would remove that window at a monthly cost.
