# Roadmap

Open work only, as of 0.9.75 (2026-10-18). How things work today is in
[architecture.md](architecture.md) and [images.md](images.md); why, in
[decisions.md](decisions.md). Calls that need a decision rather than work are
under [Open decisions](#open-decisions).

## Node daemon (`ucloud-noded`)

- **File writes and archive uploads in noded.** Still served by the Python agent; the
  largest setup cost left at 512 rollouts (`node_agent.py`, `runtime/noded/src/exec_front.rs`).
- **Park, wake, migration, commit, reconcile and heartbeats in noded**, then delete the
  Python agent (`direct_service.py`, `direct_warden.py`, `hibernation.py`, `node_agent.py`,
  `heartbeat_sender.py`).
- **Create-path tails.** `runsc_create`, storage prepare (`mkfs.xfs` per create) and
  registry commit set node-side create p95 (`runtime/noded/src/`: `create.rs`, `storage.rs`, `registry/`).
- **CPU QoS for background work** (`cpu.idle`/`cpu.weight` for prefetch, publication, offload, cache fill).
- **IPv6 off on the agent's relay-egress veths** (`direct_network.py`).
- **Drop the retired noded flag keys** (`direct_node_front_door`, `direct_node_rust_create/exec/pause`,
  `direct_pause_tier`, `direct_local_model_waits`, `direct_split_memory_backing`) from
  deployment files and their true-only check in `config.py`, once no rollback to 0.10.0 is possible.

## Admission

- **Last two gates for Rust admission:** a mid-burst drain must not report ready early, and
  growth admission at ~180 sandboxes/node must stay under 1 s (`scripts/bench/bench_rl_scale.py`,
  `scripts/bench/live_relay_load_benchmark.py`, flag `sandbox.direct_node_rust_admission`).
- **Delete the Python admission ledger** one release after the flag is on
  (`transition_admission.py`, ledger/pool/growth code in `direct_service.py`, `DemandPublisher`,
  `agent-demand.json`). `admission.py` stays for the gateway. Exec already decides
  without the ledger (`admission_decision.exec_refusal`).
- **Drop gateway wake admission** (`wake_admission.py`, `resource_admission.py`); the node admits.

## Writable state and memory

- **Hibernate cost independent of bytes written** (gVisor patch 0008, external filestore);
  today a park after large writes takes tens of seconds (`runtime/gvisor/`, `direct_warden.py`).
- **One writable backing:** node-local XFS project quotas and reflink instead of per-sandbox
  ublk, `mkfs.xfs` and the growth poller (`storage_native_*.py`, `runtime/storage_native/`).

## RL state primitives

- **Commit, gateway half:** route, `image_commits` table, reconciler and SDK call on top of the
  existing worker export (`node_commit.py`, `commit_steps.py`, builder `publish_commit`).
- **Fork and templates:** not started; open questions are partial forks and cross-deployment commits.
- **Node-side hydration:** warm a worker's cache for a planned group before its creates land
  (`environment_backend.py`, new node route).

## Images and chunk store

- **Python off the image data path:** serve toolkits and unconverted EROFS through nydusd, then
  delete `environment_nbd.py`, `environment_cache.py` and the RAFS reader in `environment_rafs.py`.
- **Docker off sandbox workers:** workers no longer use Docker's image store. Left: release the
  `sandbox.docker_quota_image_gb` reservation on fresh workers (the image cache root lives on that
  mount), an explicit FORWARD DROP policy and another `tini` before dockerd can stop, and deleting
  `DockerOverlay2RootfsStore` (`image_rootfs.py`) once builders stop using it.
- **No per-create init copy:** mount `/.ucloud-init` read-only (`runtime/noded/src/guest.rs`).
- **Placement by chunk residency** (`gateway/heartbeats.py`, `placement_choice.py`).
- **S3 outages** still fail commits and fills until S3 recovers (stalls are bounded since 0.9.67).

## Scaling and control plane

- **Cluster create rate:** target ≥ 300 creates/s; ~30/s measured at production's worker cap.
- **Cold start:** ready p50 ~2 min from zero; add image-gated scale-up (`policy.py`, `node_agent.py`).
- **Finish power-of-k:** move wakes to node-gated admission and delete the placement service.
- **Routing errors by type:** catch `RoutingUnavailable` instead of `sqlite3.DatabaseError`
  (`control_plane.py`, `gateway/heartbeats.py`), then drop that base class (kept for 0.10.x).
- **Stateless exec ingress:** add route tokens for events and files. Drop the unused
  `exec_sessions` and `exec_losses` tables once no 0.10.0 process shares the routing schema.
- **Split the single gateway host:** first move heartbeats, quarantine, host locks, build
  history and autoscaler state from host-local files into PostgreSQL.
- **Node-loss timers:** run the observe-only experiment; gaps: no alert on `unavailable`, untimed
  suspensions re-quarantined, occupied never-returning workers need an operator.

## Relay

- **Packet-watcher cost at 500 sandboxes/node** is unmeasured (one NFLOG message per packet).
- **Resource-phase hints, client half:** the SDK still ships `update_resource_phase` and
  verifiers-ucloud `resource_phase_hints`; drop them in their next releases.

## Open decisions

- **Turn on `sandbox.direct_node_rust_admission`** once the two gates pass.
- **Retire relay-driven park and wake** once every trainer sets `guest_relay_url`.
- **Registry disk guard on a shared drive:** replace `statvfs` percentages with a byte bound, or turn it off.
- **One memory mode:** pick one of split, RAM or reflink-restore backing and delete the rest.
- **A second store node:** removes the ~1.5 h cache-refill window after a loss, at a monthly cost.
